from acme_api.directory import employee_dn


def test_employee_dn_escapes_special_characters():
    assert employee_dn("Lovelace, Ada", "engineering") == "cn=Lovelace\\, Ada,ou=engineering,ou=people,dc=acme,dc=com"
