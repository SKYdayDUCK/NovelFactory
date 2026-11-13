"""Serialize authorized context before volatile dispatch identity for prefix reuse.

This changes wire order only. Authorization, source selection, immutable snapshot
hashes and output validation remain the gateway's responsibility.
"""

from novel_factory.domain.canonical import canonical_bytes


def material_json(packet):
    # Keep large source sections ahead of task-specific assignment/diagnostics.
    # Canonicalize each value, preserving every field and all array ordering.
    first = ("authorized_materials", "creative_materials", "structured_evidence",
             "response_references")
    keys = [key for key in first if key in packet]
    keys += sorted(set(packet) - set(keys) - {"assignment"})
    if "assignment" in packet:
        keys.append("assignment")
    return "{" + ",".join(canonical_bytes(key).decode() + ":" + canonical_bytes(packet[key]).decode()
                          for key in keys) + "}"


def role_messages(system, packet, identity, *, exact_envelope=False):
    suffix = "Task response binding. Copy these identifying fields exactly.\nIdentity: " + canonical_bytes(identity).decode()
    if exact_envelope:
        envelope = {key: value for key, value in identity.items() if key != "dependency_fingerprint"}
        envelope.update(status="SUCCEEDED", unresolved_questions=[], payload={})
        suffix += "\nOuter JSON template: " + canonical_bytes(envelope).decode()
    return ({"role": "system", "content": system},
            {"role": "user", "content": material_json(packet)},
            {"role": "system", "content": suffix})
