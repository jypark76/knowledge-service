# In plain English: these tests check the list of database changes (the Liquibase
# "changelog" in db/changelog) BEFORE anything runs. Each change is a small SQL file,
# and one root file lists them in order. Liquibase only runs what the root file
# lists, so a change file that is missing from the list would silently never run.
# These tests need no database and no Docker.
import re
from pathlib import Path

import yaml

CHANGELOG = Path(__file__).resolve().parent.parent / "db" / "changelog"
ROOT_FILE = CHANGELOG / "changelog-root.yaml"


# In plain English: the SQL change files, in the order their numbers say.
def migration_files():
    return sorted(CHANGELOG.glob("*.sql"))


# In plain English: the files the root changelog lists, in the order it lists them.
def listed_files():
    root = yaml.safe_load(ROOT_FILE.read_text(encoding="utf-8"))
    return [entry["include"]["file"] for entry in root["databaseChangeLog"]]


# In plain English: every change file must be on the root list, and in number order.
# A file missing from the list would never run anywhere, and nothing would complain.
# This is the same mistake as a test-file list that goes stale.
def test_the_root_changelog_lists_every_migration_in_order():
    on_disk = [path.name for path in migration_files()]
    assert on_disk, "there are no migration files at all"
    assert listed_files() == on_disk


# In plain English: each change file must be in Liquibase's SQL format: the first
# line says so, and each change has an author and an ID. IDs must be unique, and the
# ID must start with the file's own number, so a file and its change cannot drift apart.
def test_every_migration_is_a_well_formed_liquibase_file():
    assert migration_files(), "there are no migration files to check"
    seen = set()
    for path in migration_files():
        text = path.read_text(encoding="utf-8")
        assert text.startswith("--liquibase formatted sql"), path.name
        ids = re.findall(r"^--changeset\s+\S+:(\S+)", text, flags=re.MULTILINE)
        assert ids, f"{path.name} has no changeset"
        for change_id in ids:
            assert change_id not in seen, f"duplicate change id {change_id}"
            seen.add(change_id)
            assert change_id.startswith(path.name.split("-")[0]), (path.name, change_id)


# In plain English: change 0001 must really do what this step promises: add the
# label column and a uniqueness rule on it, so the same label cannot be saved twice.
def test_migration_0001_adds_the_label_column_and_its_uniqueness_rule():
    text = (CHANGELOG / "0001-add-source-submission-id.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN source_submission_id uuid" in text
    assert "CREATE UNIQUE INDEX" in text
    assert "(source_submission_id)" in text


# In plain English: the repo is public, so the change files must never hold a
# password. The migration job gets the admin password from the Kubernetes Secret.
def test_the_changelog_holds_no_passwords():
    for path in CHANGELOG.iterdir():
        assert "password" not in path.read_text(encoding="utf-8").lower(), path.name
