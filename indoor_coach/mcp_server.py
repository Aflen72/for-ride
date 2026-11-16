"""Optional official MCP SDK stdio server. No external mutation tools exposed."""
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .service import Service
from .evidence import AGENT_WORKFLOW_PROMPT

mcp = FastMCP('Indoor Cycling Coach', instructions=AGENT_WORKFLOW_PROMPT)
_service = None

def call(name, arguments):
    global _service
    if _service is None:
        _service=Service(os.environ.get('INDOOR_COACH_DB',str(Path.home()/'.indoor-coach'/'coach.sqlite3')))
    try:
        return _service.call(name,arguments)
    except Exception:
        # Never expose raw connector exceptions or submitted records to SDK logs.
        return {'error':'Operation failed. Check schema, date range, or local configuration. No external write performed.'}

@mcp.prompt()
def evidence_first_review() -> str:
    """Data-first workflow: inspect authorized zones and comparable history before advice."""
    return AGENT_WORKFLOW_PROMPT

@mcp.tool()
def prepare_analysis(as_of: str, ftp: float | None = None, target_id: str | None = None, history_days: int = 28, refresh: bool = True) -> dict:
    """FIRST STEP before advice: check authorized ICU once if configured, then inspect local activity-specific HR zones and comparable history. Missing evidence and unsupported capabilities are explicit. Does not fetch current athlete settings or streams."""
    return call('prepare_analysis',{'as_of':as_of,'ftp':ftp,'target_id':target_id,'history_days':history_days,'refresh':refresh})

@mcp.tool()
def import_activities(records: list[dict]) -> dict:
    """Save normalized activities locally; does not upload or change remote accounts."""
    return call('import_activities',{'records':records})

@mcp.tool()
def import_wellness(records: list[dict]) -> dict:
    """Save user-provided daily wellness locally. Missing data must be null or omitted."""
    return call('import_wellness',{'records':records})

@mcp.tool()
def import_baseline(record: dict) -> dict:
    """Save manual prior-seven-day summary with period_start/end and sample_count."""
    return call('import_baseline',{'record':record})

@mcp.tool()
def sync_icu(oldest: str, newest: str) -> dict:
    """Read authorized Intervals.icu records into local SQLite. Dates YYYY-MM-DD. Requires local environment credentials; never pass key in arguments."""
    return call('sync_icu',{'oldest':oldest,'newest':newest})

@mcp.tool()
def analyze(as_of: str, ftp: float | None = None) -> dict:
    """Review cached evidence; no fresh network query. Use prepare_analysis before answering new training questions. FTP is user-supplied watts."""
    return call('analyze',{'as_of':as_of,'ftp':ftp})

@mcp.tool()
def propose_plan(as_of: str, ftp: float | None = None) -> dict:
    """Save next-day and seven-day DRAFT only. Use preview_icu for export; external execution requires independent user CLI."""
    return call('propose_plan',{'as_of':as_of,'ftp':ftp})

@mcp.tool()
def list_proposals() -> list | dict:
    """Read local proposals, none of which has been externally scheduled."""
    return call('list_proposals',{})

@mcp.tool()
def preview_icu(proposal_id: str, account: str, timezone: str, ftp: float, selection: str = 'next') -> dict:
    """Read-only minimal export, never approval or execution. Save result JSON for independent user review."""
    return call('preview_icu', {'proposal_id':proposal_id,'account':account,'timezone':timezone,'ftp':ftp,'selection':selection})

def main(): mcp.run(transport='stdio')

if __name__=='__main__': main()
