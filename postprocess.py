# Read XMI standoff annotation file and convert it into a XML file similar to TEI with standoff annotation for relationships
# Export the files from Inception as UIMA CAS XMI (XML 1.1) and put them (unzipped!) in the folder named unter "infiles"
# Important: This code assumes no intersecting entities!

import glob
import json
import os
import re
from lxml import etree as et
from utils.text_modification import modify_text
from utils.small_corrections import small_corrects
import pprint as pp
#from xml.sax.saxutils import escape


def get_node_priority(node):
    """
    This makes sure that when sorting the spans, desc-Spans will be processed BEFORE 
    mentions, values, etc. 
    This is relevant especially for desc-spans that completely overlap with an
    entity mention or value, such as is often the case for desc.owner.

    This function may need expansion later on if more such cases exist.
    """
    try:
        l = node.get("label").lower().split(".")[0]
    except:
        return 1
    if l == "desc":
        return 0
    #elif l == "head":
    #    return 2
    else:
        return 1


def create_node_tree(in_root, document_text, start_index_dict, end_index_dict):
    """
    This node tree is mostly just as a help, but the code may probably easily be adopted to port everything to a TEI-format.
    """
    spans = in_root.findall(".//custom:Span", namespaces={"custom":"http:///custom.ecore"})
    # note which entity and which tag, start or end, needs to be inserted at this point
    sorted_spans = []
    for ent in spans:
        sorted_spans.append((ent, int(ent.get("begin")), int(ent.get("end")), get_node_priority(ent)))
    sorted_spans.sort(key=lambda x: (x[1], -x[2], x[3]))
    work_root = et.Element("XML", nsmap={"custom":"http:///custom.ecore", "cas":"http:///uima/cas.ecore"})
    parent_node = work_root
    for entity, start, end, _ in sorted_spans:
        # classify if span is entity, attribute or description
        label = entity.get('label')
        if label == None:
            print(f"WARNING: Empty Label in node with id {entity.get('{http://www.omg.org/XMI}id')}!")
            label = ""
        label = label.lower().split(".")
        span_type = ""
        if label[0] in SCHEMA_INFO["mention_classes"]:
            span_type = "ent"
            if label[0] == "unk":
                label = ["unk", "unk"]
        elif label[0] == "lst":
            span_type = "lst"
        elif label[0] == "att":
            span_type = "att"
        elif label[0] == "desc":
            span_type = "desc"
        elif label[0] == "head":
            span_type = "head"
        elif label[0] in SCHEMA_INFO["value_tags"]:
            span_type = "value"
        elif label[0] in SCHEMA_INFO["other_tags"]:
            # TODO: Implement deletion and moving by htr tags
            if label[0] == "unclear":
                print(f"WARNING: Unclear Label encountered in node with id {entity.get('{http://www.omg.org/XMI}id')}!")
            span_type = ""
            continue
        else:
            print(f"ERROR: Unrecognized Span Label '{label[0]}' in annotation with id {entity.get('{http://www.omg.org/XMI}id')}!")
            continue
        label = ".".join(label)

        # transform character index to token index
        token_start = start_index_dict[start]
        try:
            token_end = end_index_dict[end]
        except KeyError:
            # Inception performs an implicit tokenization, which allows annotations
            # to be set outside our own preprocessing. This can lead to annotations
            # ending inside tokens as defined by our preprocessing/system
            # to circumvent this problem, we simply stretch the tag to the end of the token
            while end not in end_index_dict:
                end += 1
            token_end = end_index_dict[end]
            
            print(f"WARNING: An annotation ended inside a token. Check this error manually for annotation with id {entity.get('{http://www.omg.org/XMI}id')}!")

        # We need to check all parent nodes above if they contain the current node
        # NOTE: We increase token_end by 1 to match common span annotation schemes (which usually mark a span of length 1 as x to x+1)
        while(parent_node != work_root):
            if token_end <= int(parent_node.get("end"))-1:
                current_node = et.SubElement(parent_node, "Entity", id=entity.get("{http://www.omg.org/XMI}id"), span_type=span_type, label=label, start=str(token_start), end=str(token_end+1), text=document_text[start:end])
                break
            else:
                parent_node = parent_node.getparent()
        else:
            current_node = et.SubElement(work_root, "Entity", id=entity.get("{http://www.omg.org/XMI}id"), span_type=span_type, label=label, start=str(token_start), end=str(token_end+1), text=document_text[start:end])
        parent_node = current_node
    
    # We get relations from three sources: relation layer, att and desc
    relations = in_root.findall(".//custom:Relation", namespaces={"custom":"http:///custom.ecore"})
    for relation in relations:
        if relation.get("label") is None:
            print(f"ERROR: Missing label for a relation {relation.get('{http://www.omg.org/XMI}id')}!")
            continue
        else:
            label = relation.get("label")
        current_node = et.SubElement(
            work_root, 
            "Relation", 
            id=relation.get("{http://www.omg.org/XMI}id"), 
            label=label,
            from_entity=relation.get("Governor"),
            to_entity=relation.get("Dependent"),
            )

    return work_root


def process_others(other_info, mention_id):
    numerus = SCHEMA_INFO["other_fields"]["numerus"][0]
    spec = SCHEMA_INFO["other_fields"]["specificity"][0]
    tempus = SCHEMA_INFO["other_fields"]["tense"][0]

    for o in other_info:
        if o in SCHEMA_INFO["other_fields"]["numerus"]:
            numerus = o
        elif o in SCHEMA_INFO["other_fields"]["specificity"]:
            spec = o
        elif o in SCHEMA_INFO["other_fields"]["tense"]:
            tempus = o
        elif o == "":
            # someone put two dots by mistake instead of one
            pass
        elif o not in SCHEMA_INFO["other_fields"]["other"]:
            print(f"ERROR: Unrecognized information {o} in Mention Debug Id {mention_id}. Ignoring it.")

    return numerus, spec, tempus

def apply_conversions(entity_type):
    for o, r in SCHEMA_INFO["conversions"]["entity_types"].items():
        entity_type = re.sub(o, r, entity_type)

    return entity_type

old_to_new_ids = {}
mention_subtypes = set()
desc_types = set()

def get_and_validate_parent_entity_type(parent, entity):
    # inherit entity type from parent
    try:
        label = parent.get('label').lower()
    except AttributeError as e:
        if parent.tag == "XML":
            print(f"ERROR: Found Attribute with mention id {entity.get('id')} that is not child of another mention. Ignoring the attribute...")
            return None
        else:
            raise e
    label = label.split(".")
    if label[0] == "lst":
        # if parent is a list, we need to get the entity classification
        # from one of the REF-child elements
        child = parent.find("./Entity[@span_type='ent']")
        if child == None:
            print("WARNING: Could not get entity class for attribute because LST-Element did not contain any REF-Elements! Setting entity class to UNK.")
            entity_type = "unk"
        else:
            entity_type = child.get('label').lower().split(".")[1]
    elif label[0] == "head":
        print(f"ERROR: Attribute with id {entity.get('id')} has a head-Element as parent. \
Using parent of head instead as parent of Attribute. Make sure to fix this as heads should contain further spans!")
        entity_type = get_and_validate_parent_entity_type(parent.getparent(), entity)
    else:
        entity_type = label[1]

    entity_type = apply_conversions(entity_type)
    return entity_type


def pro_coref_get_entity_type(work_root, coref, mention_type):
    parent = work_root.find(f".//Entity[@id='{coref.get('to_entity')}']")
    if parent.get("span_type") == "head":
        parent = parent.getparent()
    while parent.get("label").split(".")[0] in ["pro", "self"] and len(parent.get("label").split(".")) == 1:
        # we keep searching until we find a non-abbreviated or non-PRO mention
        coref = work_root.find(f".//Relation[@from_entity='{parent.get('id')}'][@label='coref']")
        parent = work_root.find(f".//Entity[@id='{coref.get('to_entity')}']")
        # a catch in case a coref was placed to a head instead of the parent tag
        if parent.get("span_type") == "head":
            parent = parent.getparent()
    # when we find the parent, we copy its entity type and ordinality, if necessary
    parentlabel = parent.get("label").split(".")
    if parentlabel[0] == "lst":
        first_child_label = parent.find("./Entity[@span_type='ent']").get("label").split(".")
        mention_type = mention_type
        other_types = ["grp"]  # lists are always groups of entities
        if first_child_label[0] in ["pro", "self"] and len(first_child_label) == 1:
            children = [c for c in parent.findall("./Entity[@span_type='ent']") if len(c.get("label").split(".")) > 1]
            if children:
                entity_type = children[0].get("label").split(".")[1]
            else:
                firstchild = parent.find('./Entity[@span_type="ent"]')
                coref = work_root.find(f".//Relation[@from_entity='{firstchild.get('id')}'][@label='coref']")
                if coref is None:
                    entity_type = "unk"
                else:
                    _, entity_type, _ = pro_coref_get_entity_type(work_root, coref, mention_type)
                # TODO: Test if this works, if we ever need this usecase
                #print("WARNING: A coreference to a list was encountered only containing PROs with coreferences. The resolution of this is yet to be implemented. Entity type of the list will be set to UNK.")
        else:
            entity_type = first_child_label[1]
    else:
        mention_type, entity_type = mention_type, parentlabel[1]
        parent_other = parentlabel[2:] if parentlabel[0] == "nam" else parentlabel[3:]
        other_types = []
        for el in parent_other:
            if el in SCHEMA_INFO["other_fields"]["numerus"]:
                other_types = [el]
                break
    
    entity_type = apply_conversions(entity_type)
    return mention_type, entity_type, other_types


def write_entities(out_root, work_root):
    global old_to_new_ids

    old_to_new_ids = {}

    entities_node = et.SubElement(out_root, "Mentions")
    token_list = out_root.findall(".//T")

    ### LISTS ###
    for entity in work_root.findall(".//Entity[@span_type='lst']"):

        mention_id = len(old_to_new_ids)
        old_to_new_ids[entity.get("id")] = mention_id

        label = entity.get("label").lower().split(".")
        if len(label) > 1:
            subtype = label[1]
        subtype = ""

        # get all child entity types
        child_entities = entity.findall("./Entity[@span_type='ent']")
        entity_types = []
        for child in child_entities:
            label = child.get("label").split(".")
            if label[0] not in ["pro", "self"] or len(label) > 1:
                entity_types.append(label[1])
            else:
                coref = work_root.find(f".//Relation[@from_entity='{child.get('id')}'][@label='coref']")
                if coref is None:
                    entity_types.append("unk")
                else:
                    mention_type, entity_type, other_types = pro_coref_get_entity_type(work_root, coref, label[0])
                    entity_type = apply_conversions(entity_type)
                    entity_types.append(entity_type)
        entity_types = ",".join(entity_types)

        et.SubElement(entities_node, 
            "List",
            mention_id=str(mention_id), 
            subtype=subtype,
            entity_types=entity_types,
            start=entity.get("start"),
            end=entity.get("end"),
            )

    ### REFERENCES ###
    for entity in work_root.findall(".//Entity[@span_type='ent']"):
        label = entity.get('label').lower()
        label = label.split(".")

        mention_subtype = ""

        if label[0] == "nam":
            mention_type, entity_type = label[:2]
            other_types = label[2:]
        elif label[0] in ["pro", "self"] and len(label) == 1:
            # this is the shortcut to get the information from a coreference.
            # first find the relevant relation/coref, then the relevant mention
            coref = work_root.find(f".//Relation[@from_entity='{entity.get('id')}'][@label='coref']")
            if coref is None:
                print(f"ERROR: PRO mention with id {entity.get('id')} encountered with no further tags, maybe a forgotten coreference is the problem? Setting entity_type to UNK to skip.")
                mention_type, entity_type = label[0], "unk"
            else:
                mention_type, entity_type, other_types = pro_coref_get_entity_type(work_root, coref, label[0])
        else:
            mention_type, entity_type = label[:2]
            if len(label) > 2:
                mention_subtype = label[2]
                other_types = label[3:]
            else:
                other_types = []

        if mention_subtype:
            mention_subtypes.add((
                mention_subtype,
                entity_type
            ))

        # Process other types
        numerus, spec, _ = process_others(other_types, entity.get("id"))
        entity_type = apply_conversions(entity_type)

        head_elem = entity.find("Entity[@label='head']")
        if head_elem == None:
            # Implizierter Head
            # print(f"Warning: Implizierter Head bei {entity.get('id')}.")
            head_start = entity.get("start")
            head_end = entity.get("end")
        else:
            head_start = head_elem.get("start")
            head_end = head_elem.get("end")

        # TODO: Decide if this reference is new or carries a coreference to a previous entity
        mention_id = len(old_to_new_ids)
        old_to_new_ids[entity.get("id")] = mention_id

        et.SubElement(entities_node, 
            "Reference",
            mention_id=str(mention_id), 
            mention_type=mention_type,
            mention_subtype=mention_subtype,
            entity_type=entity_type,
            numerus=numerus,
            specificity=spec,
            start=entity.get("start"),
            end=entity.get("end"),
            head_start=head_start,
            head_end=head_end,
            head_text=" ".join([t.text for t in token_list[int(head_start):int(head_end)]])
            )
    
    for entity in work_root.findall(".//Entity[@span_type='att']"):

        parent = entity.getparent()
        entity_type = get_and_validate_parent_entity_type(parent, entity)
        if entity_type == None:
            continue

        # get own information
        label = entity.get('label').lower()
        label = label.split(".")

        mention_type = "nom" if "pro" not in label[2:] else "pro"
        mention_subtype = label[1]
        if mention_subtype == "alias":
            mention_type = "nam"
        numerus, spec, _ = process_others(label[2:], entity.get("id"))
        entity_type = apply_conversions(entity_type)

        mention_subtypes.add((
            mention_subtype,
            entity_type
        ))

        head_elem = entity.find("Entity[@label='head']")
        if head_elem == None:
            # Implizierter Head
            #print(f"Warning: Implizierter Head bei {entity.get('id')}.")
            head_start = entity.get("start")
            head_end = entity.get("end")
        else:
            head_start = head_elem.get("start")
            head_end = head_elem.get("end")

        # TODO: Give this the reference id of the parent element
        mention_id = len(old_to_new_ids)
        old_to_new_ids[entity.get("id")] = mention_id

        et.SubElement(entities_node, 
            "Attribute",
            mention_id=str(mention_id), 
            mention_type=mention_type,
            mention_subtype=mention_subtype,
            entity_type=entity_type,
            numerus=numerus,
            specificity=spec,
            start=entity.get("start"),
            end=entity.get("end"),
            head_start=head_start,
            head_end=head_end,
            head_text=" ".join([t.text for t in token_list[int(head_start):int(head_end)]])
            )
        
    # NOTE: Should we only add those descriptors that are NOT also relations?
    description_node = et.SubElement(out_root, "Descriptors")    
    for desc in work_root.findall(".//Entity[@span_type='desc']"):
        label = desc.get('label').lower()
        label = label.split(".")
        try:
            desc_type = label[1]
        except IndexError as e:
            if len(label) == 1:
                print(f"WARNING: Missing desc-Categorization in description with id {desc.get('id')}.\
                     Setting it as UNK for the moment.")
                desc_type = "unk"
                desc.set('label', desc_type + ".unk")

        desc_types.add((
            desc_type,
        ))
        et.SubElement(description_node, 
            "Descriptor",
            desc_type=desc_type,
            start=desc.get("start"),
            end=desc.get("end"),
            text=" ".join([t.text for t in token_list[int(desc.get("start")):int(desc.get("end"))]])
            )
        

def write_values(out_root, work_root):
    value_node = et.SubElement(out_root, "Values")
    token_list = out_root.findall(".//T")
    for value in work_root.findall(".//Entity[@span_type='value']"):
        et.SubElement(value_node, 
            "Value",
            value_type=value.get("label"),
            start=value.get("start"),
            end=value.get("end"),
            text=" ".join([t.text for t in token_list[int(value.get("start")):int(value.get("end"))]])
            )


def write_relations(out_root, work_root):
    relations_node = et.SubElement(out_root, "Relations") 

    # First, the easy ones that were tagged as relations
    for relation in work_root.findall(".//Relation"):
        label = relation.get("label").lower().split(".")
        rel_type = label[0]
        _, _, tense = process_others(label[1:], relation.get("id"))
        try:
            et.SubElement(relations_node, 
                "Relation",
                rel_type=rel_type,
                tense=tense,
                from_mention=str(old_to_new_ids[relation.get("from_entity")]),
                to_mention=str(old_to_new_ids[relation.get("to_entity")]),
                )
        except KeyError as e:
            print(f"ERROR: When trying to write a relation, a mention id could not be found: {e}. Maybe the relation was connected to an invalid annotation such as a 'desc.xy'?")
        
    for entity in work_root.findall(".//Entity[@span_type='ent']"):
        label = entity.get('label').lower()
        label = label.split(".")
        if label[0] == "nam" or len(label) < 3:
            continue
        rel_type = label[2]
        _, _, tense = process_others(label[3:], entity.get("id"))

        # check if an entity is included in this span, then this is what the relationship refers to
        # if there is no entity included, it's not a relationship
        child_entities = entity.findall("./Entity[@span_type='ent']")
        
        for child_entity in child_entities:
            try:
                et.SubElement(relations_node, 
                    "Relation",
                    rel_type=rel_type,
                    tense=tense,
                    from_mention=str(old_to_new_ids[entity.get("id")]),
                    to_mention=str(old_to_new_ids[child_entity.get("id")]),
                    )
            except KeyError as e:
                print(f"ERROR: When trying to write a relation, a mention id could not be found: {e}. Maybe the relation was connected to an invalid annotation such as a 'desc.xy'?")
    
    # now the implied relations from att and desc (and entities which are PRO and NOM possibly!)
    # basically, if there is another mention inside an att or a desc, we have a relation between the original mention and the one inside
    for entity in work_root.findall(".//Entity[@span_type='att']"):
        label = entity.get('label').lower()
        label = label.split(".")
        rel_type = label[1]
        _, _, tense = process_others(label[2:], entity.get("id"))

        # check if an entity is included in this span, then this is what the relationship refers to
        # if there is no entity included, it's not a relationship
        child_entities = entity.findall("./Entity[@span_type='ent']")

        for child_entity in child_entities:
            try:
                et.SubElement(relations_node, 
                    "Relation",
                    rel_type=rel_type,
                    tense=tense,
                    from_mention=str(old_to_new_ids[entity.get("id")]),
                    to_mention=str(old_to_new_ids[child_entity.get("id")]),
                    )
            except KeyError as e:
                print(f"ERROR: When trying to write a relation, a mention id could not be found: {e}. Maybe the relation was connected to an invalid annotation such as a 'desc.xy'?")
    
    # desc work almost the same as att, but the connected id is that of the parent element instead
    for descriptor in work_root.findall(".//Entity[@span_type='desc']"):
        parent = descriptor.getparent()
        if parent.tag == "XML":
            print(f"ERROR: A Desc-Span is standing independently. Check span id {descriptor.get('id')}. Skipping this potential relation.")
            continue
        label = descriptor.get('label').lower()
        label = label.split(".")
        rel_type = label[1]
        _, _, tense = process_others(label[2:], descriptor.get("id"))

        # check if an entity is included in this span, then this is what the relationship refers to
        # if there is no entity included, it's not a relationship
        child_entities = descriptor.findall("./Entity[@span_type='ent']")
        
        for child_entity in child_entities:
            try:
                et.SubElement(relations_node, 
                    "Relation",
                    rel_type=rel_type,
                    tense=tense,
                    from_mention=str(old_to_new_ids[parent.get("id")]),
                    to_mention=str(old_to_new_ids[child_entity.get("id")]),
                    )
            except KeyError as e:
                print(f"ERROR: When trying to write a relation, a mention id could not be found: {e}. Maybe the relation was connected to an invalid annotation such as a 'desc.xy'?")


def write_text(text_elem, text):
    """
    Text string is transformed into single token elements.
    We use line elements to keep some of the original document structure intact.
    We also return start and end dictionaries to make matching the tokens
    to the annotations easier in the next steps.

    NOTE: THIS DOES NOT PERFORM ANY "PROPER" PREPROCESSING!
    """
    lines = text.split("\n")
    start_index_dict = {}
    end_index_dict = {}
    current_index = 0
    j = 0
    for i, line in enumerate(lines):
        if not line and i+1 == len(lines):  # remove empty trailing strings
            continue
        line_elem = et.SubElement(text_elem, "L", line_id=str(i))
        tokens = line.split(" ")
        for token in tokens:
            start_index_dict[current_index] = j
            token_elem = et.SubElement(line_elem, "T", token_id=str(j))
            token_elem.text = token
            current_index += len(token)
            end_index_dict[current_index] = j
            current_index += 1  # for the whitespace we removed earlier
            j += 1
    return start_index_dict, end_index_dict

def process_xmi_zip(filename, xmi_file):
    in_root = et.fromstring(xmi_file)

    at_least_one_span = in_root.find("./custom:Span", namespaces={"custom":"http:///custom.ecore"})
    if at_least_one_span is None:
        # stop processing if document doesn't contain annotations
        return

    print(f"Processing {filename}.")

    outname = filename.replace(".txt", ".xml")

    process_general(in_root, outname)


def process_xmi(xmi_file):
    print(f"Processing {xmi_file}.")

    infile = et.parse(xmi_file)
    outname = os.path.basename(xmi_file).replace(".xmi", ".xml")
    in_root = infile.getroot()

    process_general(in_root, outname)


def process_general(in_root, outname):

    # Modify the CAS XMI according to htr.xy tags
    in_root = modify_text(in_root)

    # Small Corrections
    in_root = small_corrects(in_root)

    text_node = in_root.find("./cas:Sofa", namespaces={"cas":"http:///uima/cas.ecore"})
    document_text = text_node.get("sofaString")

    # TODO: Write DocumentMetaData
    out_root = et.Element("XML")
    out_text = et.SubElement(out_root, "Text")
    start_index_dict, end_index_dict = write_text(out_text, document_text)

    work_root = create_node_tree(in_root, document_text, start_index_dict, end_index_dict)
   
    # For debugging seeing the trees might be helpful, so we keep the option in to write them
    #work_tree = et.ElementTree(work_root)
    #work_tree.write(os.path.join(DEBUGFOLDER, outname), xml_declaration=True, pretty_print=True, encoding="utf8")

    write_entities(out_root, work_root)
    write_values(out_root, work_root)
    write_relations(out_root, work_root)
    # TODO: Write Events

    out_tree = et.ElementTree(out_root)
    out_tree.write(os.path.join(OUTFOLDER, outname), xml_declaration=True, pretty_print=True, encoding="utf8")

SCHEMA_INFO = None
def read_schema():
    global SCHEMA_INFO

    with open("schema_info.json", mode="r", encoding="utf8") as inf:
        SCHEMA_INFO = json.load(inf)

read_schema()
OUTFOLDER = "./outfiles/"
# DEBUGFOLDER = "debugfiles/"

if __name__ == "__main__":

    infiles = sorted(glob.glob("data/testdata/*.xmi"))

    for infile in infiles:
        process_xmi(infile)