"""Serialize authorized context before volatile dispatch identity for prefix reuse.

This changes wire order only. Authorization, source selection, immutable snapshot
hashes and output validation remain the gateway's responsibility.
"""

from novel_factory.domain.canonical import canonical_bytes


def _creative_json(materials):
    if not isinstance(materials, dict):
        # Approved context recipes express section priority as an array.
        return canonical_bytes(materials).decode()
    # A revised prior draft must not break reuse of the same scene/style facts.
    # This is wire ordering only; each nested value keeps its canonical bytes.
    keys = sorted(materials.keys() - {"prose"})
    if "prose" in materials:
        keys.append("prose")
    return "{" + ",".join(canonical_bytes(key).decode() + ":" + canonical_bytes(materials[key]).decode()
                          for key in keys) + "}"


def material_json(packet):
    # Keep large source sections ahead of task-specific assignment/diagnostics.
    # Canonicalize each value, preserving every field and all array ordering.
    first = ("authorized_materials", "creative_materials", "structured_evidence",
             "response_references")
    keys = [key for key in first if key in packet]
    keys += sorted(set(packet) - set(keys) - {"assignment"})
    if "assignment" in packet:
        keys.append("assignment")
    return "{" + ",".join(canonical_bytes(key).decode() + ":" +
                          (_creative_json(packet[key]) if key == "creative_materials" else canonical_bytes(packet[key]).decode())
                          for key in keys) + "}"


def role_messages(system, packet, identity, *, exact_envelope=False):
    suffix = ("Story completion policy: finish the story with coherent plot and character logic. "
              "Word counts, chapter lengths and estimated manuscript length are advisory only. "
              "Do not require revision, reject a story or add chapters solely to meet a length target. "
              "Keep factual, character-knowledge and literary review requirements; preserve complete output.\n"
              "Task response binding. Copy these identifying fields exactly.\nIdentity: " + canonical_bytes(identity).decode())
    if exact_envelope:
        envelope = {key: value for key, value in identity.items() if key != "dependency_fingerprint"}
        envelope.update(status="SUCCEEDED", unresolved_questions=[], payload={})
        suffix += "\nOuter JSON template: " + canonical_bytes(envelope).decode()
    return ({"role": "system", "content": system},
            {"role": "user", "content": material_json(packet)},
            {"role": "system", "content": suffix})
