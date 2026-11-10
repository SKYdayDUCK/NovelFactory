from novel_factory.fixtures import fixture_definition

def test_three_fixture_identities_and_original_days_are_kept():
    r1,legacy,bridge=[fixture_definition(n) for n in ("r1_sword","legacy_sword","white_bridge")]
    assert len({r1["namespace"],legacy["namespace"],bridge["namespace"]})==3
    assert r1["ids"]["sword"]!=legacy["ids"]["sword"]
    assert r1["expected"]["destroy_day"]==40
    assert legacy["expected"]["destroy_day"]==31
    assert bridge["expected"]["initial_consignment"]=="100"
    assert r1==fixture_definition("r1_sword")
