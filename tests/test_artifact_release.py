"""Phase 5 governed artifact release, bundle integrity, and idempotency contracts."""
from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hybridagent.artifacts.bundles import (
    ArtifactBundleError,
    build_release_bundle,
    canonical_json_bytes,
    verify_release_bundle,
)
from hybridagent.artifacts.service import ArtifactServiceError, ArtifactStudio
from hybridagent.checkpoints import CheckpointRegistry
from hybridagent.claims import ClaimLedger
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.reviews import ReviewRegistry
from hybridagent.workspaces import WorkspaceDirectory
from tests.artifact_helpers import PNG, ArtifactScope, artifact_document, scope


def _race_artifact_release(
    database: str,
    organization_id: str,
    workspace_id: str,
    version_id: str,
    run_id: str,
    checkpoint_id: str,
    owner_id: str,
    barrier,
    results,
) -> None:
    store = Store(Path(database))
    studio = ArtifactStudio(store)
    barrier.wait()
    try:
        release = studio.release_version(
            organization_id,
            workspace_id,
            version_id,
            formats=("json", "markdown"),
            run_id=run_id,
            checkpoint_id=checkpoint_id,
            created_by=owner_id,
            idempotency_key="cross-process-release",
        )
    except ArtifactServiceError as exc:
        results.put(("error", str(exc)))
    else:
        results.put(("released", release.release_id))
    finally:
        store.close()


def _reviewed_version(value: ArtifactScope):
    studio = ArtifactStudio(value.store)
    version = studio.create_version(
        value.organization_id,
        value.workspace_id,
        artifact_document(value),
        created_by=value.owner_id,
        assets={"figure-asset-1": PNG},
    )
    reviews = ReviewRegistry(value.store, checkpoints=CheckpointRegistry(value.store))
    review = reviews.request_review(
        value.organization_id,
        value.workspace_id,
        created_by=value.owner_id,
        review_type="professional_release",
        required_role="reviewer",
        subject={
            "artifact_id": version.artifact_id,
            "version_id": version.version_id,
            "document_sha256": version.document_hash,
        },
        run_id=value.run.run_id,
        interrupt_run=False,
    )
    reviews.submit_decision(
        value.organization_id,
        value.workspace_id,
        review.review_id,
        reviewer_user_id=value.reviewer_id,
        decision="approved",
        payload={"summary": "Exact artifact version approved for release."},
    )
    signature = studio.sign_version(
        value.organization_id,
        value.workspace_id,
        version.version_id,
        review_id=review.review_id,
        signer_user_id=value.reviewer_id,
        role="reviewer",
        meaning="approved for professional release",
    )
    return studio, version, review, signature


def _release(studio: ArtifactStudio, value: ArtifactScope, version_id: str, *, key: str):
    return studio.release_version(
        value.organization_id,
        value.workspace_id,
        version_id,
        formats=("json", "markdown"),
        run_id=value.run.run_id,
        checkpoint_id=value.run.head_checkpoint_id,
        created_by=value.owner_id,
        idempotency_key=key,
    )


def _bundle_with_invalid_document(bundle: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(bundle), "r")
    infos = source.infolist()
    members = {info.filename: source.read(info.filename) for info in infos}
    source.close()
    invalid_document = b"{}"
    manifest = json.loads(members["manifest.json"])
    invalid_hash = hashlib.sha256(invalid_document).hexdigest()
    manifest["document_sha256"] = invalid_hash
    manifest["release"]["document_sha256"] = invalid_hash
    for entry in manifest["files"]:
        if entry["path"] == "artifact/document.json":
            entry["sha256"] = invalid_hash
            entry["size"] = len(invalid_document)
    members["artifact/document.json"] = invalid_document
    members["manifest.json"] = canonical_json_bytes(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info in infos:
            archive.writestr(info, members[info.filename])
    return output.getvalue()


def _bundle_with_noncanonical_document(bundle: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(bundle), "r")
    infos = source.infolist()
    members = {info.filename: source.read(info.filename) for info in infos}
    source.close()
    document = b"\n" + members["artifact/document.json"] + b" \n"
    manifest = json.loads(members["manifest.json"])
    for entry in manifest["files"]:
        if entry["path"] == "artifact/document.json":
            entry["sha256"] = hashlib.sha256(document).hexdigest()
            entry["size"] = len(document)
    members["artifact/document.json"] = document
    members["manifest.json"] = canonical_json_bytes(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info in infos:
            archive.writestr(info, members[info.filename])
    return output.getvalue()


def test_governed_release_is_self_verifying_durable_and_tenant_concealed(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio, version, review, signature = _reviewed_version(value)
    release = _release(studio, value, version.version_id, key="release-final-v1")

    assert release.idempotency_key == "release-final-v1"
    assert release.formats == ("json", "markdown")
    assert release.review_ids == (review.review_id,)
    assert release.signature_ids == (signature.signature_id,)
    assert ArtifactStudio.verify_release(release) == release.manifest
    manifest = verify_release_bundle(release.bundle)
    assert manifest["release"]["release_id"] == release.release_id
    assert manifest["release"]["version_id"] == version.version_id
    assert manifest["document_sha256"] == version.document_hash

    value.store.close()
    reopened = ArtifactStudio(type(value.store)(tmp_path / "praxis.db"))
    durable = reopened.get_release(
        value.organization_id, value.workspace_id, release.release_id
    )
    assert durable is not None and durable.bundle_hash == release.bundle_hash

    organizations = OrganizationDirectory(reopened.store)
    other_org, other_owner = organizations.bootstrap("Other Practice", "other@example.com")
    other_workspace = WorkspaceDirectory(reopened.store).create(
        other_org.organization_id,
        "OTHER-RELEASE",
        "matter",
        "Other release",
        owner_user_id=other_owner.user_id,
    )
    assert reopened.get_release(
        other_org.organization_id, other_workspace.workspace_id, release.release_id
    ) is None
    reopened.store.close()


def test_release_is_idempotent_and_conflicting_key_fails_closed(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio, version, _, _ = _reviewed_version(value)
    first = _release(studio, value, version.version_id, key="release-repeat")
    second = _release(studio, value, version.version_id, key="release-repeat")
    assert second.release_id == first.release_id
    assert second.bundle == first.bundle

    checkpoint = CheckpointRegistry(value.store).checkpoint(
        value.organization_id,
        value.workspace_id,
        value.run.run_id,
        actor_id=value.owner_id,
        state={"artifact_id": version.artifact_id, "stage": "release-retry"},
        expected_head_checkpoint_id=value.run.head_checkpoint_id,
    )
    with pytest.raises(ArtifactServiceError, match="idempotency"):
        studio.release_version(
            value.organization_id,
            value.workspace_id,
            version.version_id,
            formats=("json", "markdown"),
            run_id=value.run.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            created_by=value.owner_id,
            idempotency_key="release-repeat",
        )


def test_concurrent_release_retries_persist_one_exact_release(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio, version, _, _ = _reviewed_version(value)

    def create() -> str:
        return _release(studio, value, version.version_id, key="concurrent-release").release_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        release_ids = list(executor.map(lambda _: create(), range(2)))
    assert len(set(release_ids)) == 1
    rows = value.store._directory_all(
        "SELECT release_id FROM artifact_releases WHERE organization_id=? "
        "AND workspace_id=? AND idempotency_key=?",
        (value.organization_id, value.workspace_id, "concurrent-release"),
    )
    assert [row["release_id"] for row in rows] == [release_ids[0]]


def test_cross_process_release_retries_persist_one_exact_release(tmp_path: Path) -> None:
    value = scope(tmp_path)
    _, version, _, _ = _reviewed_version(value)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    args = (
        str(tmp_path / "praxis.db"),
        value.organization_id,
        value.workspace_id,
        version.version_id,
        value.run.run_id,
        value.run.head_checkpoint_id,
        value.owner_id,
        barrier,
        results,
    )
    processes = [context.Process(target=_race_artifact_release, args=args) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in processes]
    assert {status for status, _ in outcomes} == {"released"}
    release_ids = {release_id for _, release_id in outcomes}
    assert len(release_ids) == 1
    rows = value.store._directory_all(
        "SELECT release_id FROM artifact_releases WHERE organization_id=? "
        "AND workspace_id=? AND idempotency_key=?",
        (value.organization_id, value.workspace_id, "cross-process-release"),
    )
    assert [row["release_id"] for row in rows] == list(release_ids)


def test_release_revalidates_claims_head_review_signature_and_active_role(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio, version, _, _ = _reviewed_version(value)

    claims = ClaimLedger(value.store)
    claims.create(
        value.organization_id,
        value.workspace_id,
        text="An unresolved material release claim.",
        material=True,
        created_by=value.owner_id,
    )
    with pytest.raises(ArtifactServiceError, match="unresolved material claims"):
        _release(studio, value, version.version_id, key="blocked-claim")

    value.store._directory_execute(
        "UPDATE professional_claims SET status='supported' WHERE organization_id=? "
        "AND workspace_id=? AND status<>'supported'",
        (value.organization_id, value.workspace_id),
    )
    value.store._directory_execute(
        "UPDATE organization_memberships SET status='disabled' "
        "WHERE organization_id=? AND user_id=?",
        (value.organization_id, value.reviewer_id),
    )
    with pytest.raises(ArtifactServiceError, match="active signer"):
        _release(studio, value, version.version_id, key="disabled-signer")


def test_only_current_head_with_exact_review_and_signature_can_release(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio, first, _, _ = _reviewed_version(value)
    second = studio.create_version(
        value.organization_id,
        value.workspace_id,
        artifact_document(
            value,
            sequence=2,
            parent_hash=first.document_hash,
            paragraph="A newer exact artifact head.",
        ),
        created_by=value.owner_id,
        assets={"figure-asset-1": PNG},
        expected_parent_version_id=first.version_id,
    )
    assert second.sequence == 2
    with pytest.raises(ArtifactServiceError, match="exact current artifact head"):
        _release(studio, value, first.version_id, key="stale-head")
    with pytest.raises(ArtifactServiceError, match="approved exact review"):
        _release(studio, value, second.version_id, key="unsigned-head")


def test_bundle_verifier_rejects_tamper_duplicate_and_traversal(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio, version, _, _ = _reviewed_version(value)
    release = _release(studio, value, version.version_id, key="tamper-probe")

    source = zipfile.ZipFile(io.BytesIO(release.bundle), "r")
    members = {name: source.read(name) for name in source.namelist()}
    source.close()
    members["artifact/document.json"] += b" "
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    with pytest.raises(ArtifactBundleError, match="unsafe member|integrity"):
        verify_release_bundle(out.getvalue())

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("../manifest.json", b"{}")
    with pytest.raises(ArtifactBundleError, match="unsafe member"):
        verify_release_bundle(out.getvalue())

    collision = io.BytesIO()
    with zipfile.ZipFile(collision, "w") as archive:
        archive.writestr("Artifact/document.json", b"{}")
        archive.writestr("artifact/document.json", b"{}")
    with pytest.raises(ArtifactBundleError, match="member set"):
        verify_release_bundle(collision.getvalue())

    oversized = io.BytesIO()
    with zipfile.ZipFile(oversized, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", b"0" * 2_000)
    import hybridagent.artifacts.bundles as bundle_module

    original_limit = bundle_module._MAX_BUNDLE_BYTES
    bundle_module._MAX_BUNDLE_BYTES = 1_000
    try:
        with pytest.raises(ArtifactBundleError, match="member set"):
            verify_release_bundle(oversized.getvalue())
    finally:
        bundle_module._MAX_BUNDLE_BYTES = original_limit


def test_release_rows_and_signatures_reject_all_mutation_verbs(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio, version, _, signature = _reviewed_version(value)
    release = _release(studio, value, version.version_id, key="immutable-release")

    for statement, params in (
        ("UPDATE artifact_signatures SET meaning='changed' WHERE signature_id=?", (signature.signature_id,)),
        ("DELETE FROM artifact_signatures WHERE signature_id=?", (signature.signature_id,)),
        ("UPDATE artifact_releases SET bundle_hash=? WHERE release_id=?", ("0" * 64, release.release_id)),
        ("DELETE FROM artifact_releases WHERE release_id=?", (release.release_id,)),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            value.store._directory_execute(statement, params)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        value.store._directory_execute(
            "INSERT OR REPLACE INTO artifact_releases "
            "SELECT * FROM artifact_releases WHERE release_id=?",
            (release.release_id,),
        )


def test_release_bundle_rebuild_is_byte_deterministic_and_duplicates_fail(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio, version, _, _ = _reviewed_version(value)
    release = _release(studio, value, version.version_id, key="deterministic-release")
    with zipfile.ZipFile(io.BytesIO(release.bundle), "r") as archive:
        governance = {
            name: json.loads(archive.read(f"governance/{name}.json"))
            for name in ("claims", "evidence", "reviews", "signatures", "run")
        }
        renders = {
            "json": archive.read("renders/document.json"),
            "markdown": archive.read("renders/document.md"),
        }
        report = json.loads(archive.read("validation/report.json"))
    rebuilt, manifest = build_release_bundle(
        release_context=release.manifest["release"],
        document=version.document,
        renders=renders,
        assets={"figure-asset-1": PNG},
        asset_media={"figure-asset-1": "image/png"},
        governance=governance,
        validation_report=report,
    )
    assert rebuilt == release.bundle
    assert manifest == release.manifest

    duplicate = io.BytesIO()
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("manifest.json", b"{}")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("manifest.json", b"{}")
    with pytest.raises(ArtifactBundleError, match="member set"):
        verify_release_bundle(duplicate.getvalue())

    with pytest.raises(ArtifactBundleError, match="artifact document is invalid"):
        verify_release_bundle(_bundle_with_invalid_document(release.bundle))

    with pytest.raises(
        ArtifactBundleError, match="artifact document is not canonical JSON"
    ):
        verify_release_bundle(_bundle_with_noncanonical_document(release.bundle))


def test_existing_phase5_database_migrates_release_idempotency_columns(tmp_path: Path) -> None:
    database = tmp_path / "legacy-phase5.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE artifact_releases ("
        "release_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, version_id TEXT NOT NULL, "
        "organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, document_hash TEXT NOT NULL, "
        "formats_json TEXT NOT NULL, manifest_json TEXT NOT NULL, "
        "validation_report_json TEXT NOT NULL, review_ids_json TEXT NOT NULL, "
        "signature_ids_json TEXT NOT NULL, run_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL, "
        "bundle_hash TEXT NOT NULL, bundle BLOB NOT NULL, created_by TEXT NOT NULL, "
        "created_ts REAL NOT NULL)"
    )
    connection.commit()
    connection.close()

    from hybridagent.persistence import Store

    store = Store(database)
    columns = {
        row["name"]: row for row in store._directory_all("PRAGMA table_info(artifact_releases)")
    }
    assert columns["idempotency_key"]["notnull"] == 1
    assert columns["request_hash"]["notnull"] == 1
    indexes = store._directory_all("PRAGMA index_list(artifact_releases)")
    assert "ux_artifact_releases_idempotency" in {row["name"] for row in indexes}
    store.close()
