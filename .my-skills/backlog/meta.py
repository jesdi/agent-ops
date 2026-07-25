"""Pure builder for project-meta.json from `gh project field-list` output. Stdlib only."""


def build_project_meta(owner, repo, project_number, project_id, fields_json):
    fields = {}
    for field in fields_json.get("fields", []):
        entry = {"id": field["id"]}
        options = field.get("options")
        if options:
            entry["options"] = {opt["name"]: opt["id"] for opt in options}
        fields[field["name"]] = entry
    return {
        "owner": owner,
        "repo": repo,
        "projectNumber": project_number,
        "projectId": project_id,
        "fields": fields,
    }
