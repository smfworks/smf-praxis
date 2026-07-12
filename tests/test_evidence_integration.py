"""Runtime wiring from ingestion/multimodal/verification into evidence substrate."""

from hybridagent.chat_agent import AgentEvent
from hybridagent.claims import ClaimLedger
from hybridagent.evidence import EvidenceRegistry
from hybridagent.ingest import ExtractedDoc, register_evidence
from hybridagent.multimodal import MediaClient
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.verifier import AnswerVerifier, VerifiedChatAgent
from hybridagent.workspaces import WorkspaceDirectory


def setup_runtime(tmp_path):
    store = Store(tmp_path / "praxis.db")
    orgs = OrganizationDirectory(store)
    org, owner = orgs.bootstrap("Practice", "owner@example.com")
    workspace = WorkspaceDirectory(store).create(
        org.organization_id, "MAT-1", "matter", "Matter",
        owner_user_id=owner.user_id)
    return store, org, owner, workspace


def test_ingestion_registers_source_version_and_exact_span(tmp_path):
    store, org, owner, workspace = setup_runtime(tmp_path)
    doc = ExtractedDoc("Extracted body", "report.txt", metadata={"path": "report.txt"})
    source, version, span = register_evidence(
        doc, b"Extracted body", store=store, organization_id=org.organization_id,
        workspace_id=workspace.workspace_id, created_by=owner.user_id,
        canonical_uri="file:///report.txt", publisher="Client",
        locator={"char_start": 0, "char_end": 14})
    assert source.workspace_id == workspace.workspace_id
    assert version.content_hash == EvidenceRegistry(store).get_version(
        org.organization_id, workspace.workspace_id, version.version_id).content_hash
    assert span.extracted_text == "Extracted body"


def test_multimodal_processing_can_register_derived_lineage(tmp_path):
    store, org, owner, workspace = setup_runtime(tmp_path)
    image = tmp_path / "image.png"
    image.write_bytes(b"not-real-image")
    doc, derived = MediaClient(mode="mock").process_with_lineage(
        image, store=store, organization_id=org.organization_id,
        workspace_id=workspace.workspace_id, created_by=owner.user_id,
        publisher="Client upload")
    assert doc.kind == "image"
    assert derived.kind == "caption"
    assert derived.content == doc.text


def test_answer_verifier_blocks_when_material_claim_ledger_is_not_ready(tmp_path):
    store, org, owner, workspace = setup_runtime(tmp_path)
    ClaimLedger(store).create(
        org.organization_id, workspace.workspace_id,
        text="Unresolved material claim", material=True, created_by=owner.user_id)
    verdict = AnswerVerifier().verify(
        "draft", "A sufficiently substantive answer.",
        claim_ledger=ClaimLedger(store), organization_id=org.organization_id,
        workspace_id=workspace.workspace_id)
    assert not verdict.approved
    assert "material_claims" in verdict.checks


def test_verified_chat_runtime_propagates_material_claim_scope(tmp_path):
    store, org, owner, workspace = setup_runtime(tmp_path)
    ledger = ClaimLedger(store)
    ledger.create(org.organization_id, workspace.workspace_id,
                  text="Unresolved", material=True, created_by=owner.user_id)

    class Inner:
        def run(self, messages, system=None):
            yield AgentEvent("final", {"text": "Professional conclusion."})

    events = list(VerifiedChatAgent(
        Inner(), claim_ledger=ledger, organization_id=org.organization_id,
        workspace_id=workspace.workspace_id, max_revisions=0).run(
            [{"role": "user", "content": "release draft"}]))
    assert any(event.type == "verification"
               and "material_claims" in event.data.get("checks", [])
               for event in events)
    assert all(event.type != "final" for event in events)
    assert all("Professional conclusion" not in str(event.data) for event in events)


def test_material_claim_preflight_blocks_all_inner_channels():
    class Blocked:
        def release_ready(self, organization_id, workspace_id):
            return False

    class Inner:
        called = False

        def run(self, messages, system=None):
            self.called = True
            yield AgentEvent("critique", {"text": "UNSUPPORTED MATERIAL CLAIM"})
            yield AgentEvent("error", {"error": "UNSUPPORTED MATERIAL CLAIM"})

    inner = Inner()
    events = list(VerifiedChatAgent(
        inner, claim_ledger=Blocked(), organization_id="org", workspace_id="ws",
        max_revisions=1).run([{"role": "user", "content": "release"}]))
    assert not inner.called
    assert [event.type for event in events] == ["verification"]
    assert events[0].data["checks"] == ["material_claims"]
    assert "UNSUPPORTED MATERIAL CLAIM" not in str(events)


def test_material_claim_readiness_flip_discards_buffered_trajectory():
    class FlipLedger:
        calls = 0

        def release_ready(self, organization_id, workspace_id):
            self.calls += 1
            return self.calls == 1

    class Inner:
        def run(self, messages, system=None):
            yield AgentEvent(
                "critique", {"nested": {"arbitrary": ["UNSUPPORTED MATERIAL CLAIM"]}})
            yield AgentEvent("final", {"text": "UNSUPPORTED MATERIAL CLAIM"})

    events = list(VerifiedChatAgent(
        Inner(), claim_ledger=FlipLedger(), organization_id="org",
        workspace_id="ws", max_revisions=0).run(
            [{"role": "user", "content": "release"}]))
    assert [event.type for event in events] == ["verification"]
    assert "UNSUPPORTED MATERIAL CLAIM" not in str(events)


def test_material_claim_readiness_error_fails_closed():
    class BrokenLedger:
        def release_ready(self, organization_id, workspace_id):
            raise RuntimeError("database unavailable")

    class Inner:
        def run(self, messages, system=None):
            raise AssertionError("inner engine must not run")
            yield

    events = list(VerifiedChatAgent(
        Inner(), claim_ledger=BrokenLedger(), organization_id="org",
        workspace_id="ws").run([{"role": "user", "content": "release"}]))
    assert [event.type for event in events] == ["verification"]
    assert events[0].data["checks"] == ["material_claims"]


def test_release_readiness_fails_for_disabled_organization(tmp_path):
    store, org, _owner, workspace = setup_runtime(tmp_path)
    ledger = ClaimLedger(store)
    assert ledger.release_ready(org.organization_id, workspace.workspace_id)
    store._directory_execute(
        "UPDATE organizations SET status='disabled' WHERE organization_id=?",
        (org.organization_id,))
    assert not ledger.release_ready(org.organization_id, workspace.workspace_id)
