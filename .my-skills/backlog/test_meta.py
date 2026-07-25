import meta


def test_build_project_meta_maps_fields_and_options():
    fields_json = {"fields": [
        {"id": "F_status", "name": "Status", "type": "ProjectV2SingleSelectField",
         "options": [{"id": "o_inbox", "name": "Inbox"}, {"id": "o_ready", "name": "Ready"}]},
        {"id": "F_impact", "name": "Impact", "type": "ProjectV2Field"},
        {"id": "F_area", "name": "Area", "type": "ProjectV2SingleSelectField",
         "options": [{"id": "o_feat", "name": "feature"}]},
    ]}
    out = meta.build_project_meta("acme", "acme/widget", 7, "PVT_x", fields_json)
    assert out["owner"] == "acme"
    assert out["repo"] == "acme/widget"
    assert out["projectNumber"] == 7
    assert out["projectId"] == "PVT_x"
    assert out["fields"]["Status"]["id"] == "F_status"
    assert out["fields"]["Status"]["options"]["Ready"] == "o_ready"
    assert out["fields"]["Impact"]["id"] == "F_impact"
    assert "options" not in out["fields"]["Impact"]
    assert out["fields"]["Area"]["options"]["feature"] == "o_feat"
