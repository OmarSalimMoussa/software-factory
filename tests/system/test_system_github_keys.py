from __future__ import annotations

import json
from pathlib import Path

import pytest
from system_fakes import dry_job

from runtime.system import github_keys

KEY = "ssh-ed25519 AAAAnew"
LIST = "gh api repos/o/p/keys"


@pytest.mark.parametrize(
    ("keys", "added", "removed"),
    [
        ([], True, []),
        ([{"id": 7, "title": github_keys.WARDEN_TITLE, "key": "ssh-ed25519 AAAAold"}], True, [7]),
        ([{"id": 8, "title": github_keys.WARDEN_TITLE, "key": KEY}], False, []),
        ([{"id": 9, "title": "someone else's", "key": "ssh-ed25519 AAAAother"}], True, []),
    ],
    ids=["none", "replaces-the-same-title", "already-there", "leaves-other-titles"],
)
def test_a_deploy_key_replaces_only_the_one_of_its_title(
    tmp_path: Path, keys: list[dict[str, object]], added: bool, removed: list[int]
) -> None:
    job = dry_job(tmp_path)
    job.host.answers[LIST] = (0, json.dumps(keys))

    github_keys.put_deploy_key(job, "gh", "o/p", github_keys.WARDEN_TITLE, KEY + " software-factory deploy", write=True)

    runs = [a for a in job.host.actions if a.startswith("run: gh api -X")]
    assert any("POST repos/o/p/keys" in r and "read_only=false" in r for r in runs) is added
    deleted = [r for r in runs if "DELETE" in r]
    assert deleted == [f"run: gh api -X DELETE repos/o/p/keys/{n} (as owner)" for n in removed]
    # A new key is added before an old one goes: the repo is never left without the warden's key.
    if added and removed:
        assert runs.index(next(r for r in runs if "POST" in r)) < runs.index(next(r for r in runs if "DELETE" in r))


def test_the_ci_secret_travels_on_stdin_never_a_command_line(tmp_path: Path) -> None:
    job = dry_job(tmp_path)
    private = Path("/secrets/factory-read.key")
    job.host.files[private] = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-body\n"

    github_keys.put_secret(job, "gh", "o/p", github_keys.READ_SECRET, private)

    assert "run: gh secret set FACTORY_READ_KEY --repo o/p (as owner)" in job.host.actions
    assert not any("secret-body" in action for action in job.host.actions)
