"""Material claim ledger and support-verification contracts."""
import pytest

from hybridagent.claims import ClaimError, ClaimLedger
from hybridagent.evidence import EvidenceRegistry
from hybridagent.extraction import ExtractionRegistry
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.workspaces import WorkspaceDirectory


def setup_claims(tmp_path):
    store = Store(tmp_path / "praxis.db")
    orgs = OrganizationDirectory(store)
    org, owner = orgs.bootstrap("Practice", "owner@example.com")
    workspace = WorkspaceDirectory(store).create(
        org.organization_id, "MAT-1", "matter", "Matter",
        owner_user_id=owner.user_id)
    evidence = EvidenceRegistry(store)
    source = evidence.create_source(
        org.organization_id, workspace.workspace_id,
        canonical_uri="https://example.test/source", publisher="Authority",
        created_by=owner.user_id)
    version = evidence.add_version(
        org.organization_id, workspace.workspace_id, source.source_id,
        content=b"facts", mime_type="text/plain", retrieved_ts=1.0,
        parser="plain", parser_version="1", parser_config={}, license="public",
        original_object_path="objects/facts", created_by=owner.user_id)
    span = ExtractionRegistry(store).add_span(
        org.organization_id, workspace.workspace_id, version.version_id,
        locator_type="document", locator={"paragraph": 1},
        extracted_text="The exact supporting fact.", created_by=owner.user_id)
    return org, owner, workspace, span, ClaimLedger(store)


def test_material_claim_requires_supported_evidence_for_release(tmp_path):
    org, owner, workspace, span, ledger = setup_claims(tmp_path)
    claim = ledger.create(
        org.organization_id, workspace.workspace_id,
        text="A material factual assertion", material=True, created_by=owner.user_id)
    assert not ledger.release_ready(org.organization_id, workspace.workspace_id)
    link = ledger.link_evidence(
        org.organization_id, workspace.workspace_id, claim.claim_id, span.span_id,
        relationship="supports", rationale="Direct quotation", created_by=owner.user_id)
    assert link.relationship == "supports"
    resolved = ledger.set_status(
        org.organization_id, workspace.workspace_id, claim.claim_id,
        status="supported", actor_id=owner.user_id)
    assert resolved.status == "supported"
    assert ledger.release_ready(org.organization_id, workspace.workspace_id)


def test_claim_cannot_be_marked_supported_without_support_link(tmp_path):
    org, owner, workspace, _, ledger = setup_claims(tmp_path)
    claim = ledger.create(
        org.organization_id, workspace.workspace_id,
        text="Unsupported", material=True, created_by=owner.user_id)
    with pytest.raises(ClaimError, match="supporting evidence"):
        ledger.set_status(
            org.organization_id, workspace.workspace_id, claim.claim_id,
            status="supported", actor_id=owner.user_id)


def test_contradicted_and_abstained_claims_block_material_release(tmp_path):
    org, owner, workspace, span, ledger = setup_claims(tmp_path)
    claim = ledger.create(
        org.organization_id, workspace.workspace_id,
        text="Disputed", material=True, created_by=owner.user_id)
    ledger.link_evidence(
        org.organization_id, workspace.workspace_id, claim.claim_id, span.span_id,
        relationship="contradicts", rationale="Conflict", created_by=owner.user_id)
    ledger.set_status(
        org.organization_id, workspace.workspace_id, claim.claim_id,
        status="contradicted", actor_id=owner.user_id)
    assert not ledger.release_ready(org.organization_id, workspace.workspace_id)
    with pytest.raises(ClaimError, match="unknown claim status"):
        ledger.set_status(
            org.organization_id, workspace.workspace_id, claim.claim_id,
            status="approved", actor_id=owner.user_id)
