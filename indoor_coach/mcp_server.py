"""Optional official MCP SDK stdio server. No external mutation tools exposed."""
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .service import Service
from .icu import ICUError
from .evidence import AGENT_WORKFLOW_PROMPT

COMPACT_USAGE = '\nFor compact responses opt into detail=summary. Default full responses preserve existing clients. List drafts with limit=20, after=next_cursor, detail=summary; retrieve one with get_proposal. To expand cached analysis use analyze(detail=full), not another refresh solely for formatting. Summary is not approval and does not remove evidence limitations.'

mcp = FastMCP('Indoor Cycling Coach', instructions=AGENT_WORKFLOW_PROMPT + COMPACT_USAGE)
_service = None

def call(name, arguments):
    global _service
    try:
        if _service is None:
            _service=Service(os.environ.get('INDOOR_COACH_DB',str(Path.home()/'.indoor-coach'/'coach.sqlite3')))
        return _service.call(name,arguments)
    except ICUError as exc:
        return {'error':exc.as_dict()}
    except Exception:
        # Never expose raw connector exceptions or submitted records to SDK logs.
        return {'error':'Operation failed. Check schema, date range, or local configuration. No external write performed.'}

@mcp.prompt()
def evidence_first_review() -> str:
    """Data-first workflow: inspect authorized zones and comparable history before advice."""
    return AGENT_WORKFLOW_PROMPT + COMPACT_USAGE

@mcp.tool()
def prepare_analysis(as_of: str, ftp: float | None = None, target_id: str | None = None, history_days: int = 28, refresh: bool = True, detail: str = "full") -> dict:
    """FIRST STEP before advice: optionally refresh authorized ICU and current Ride indoor FTP (unless explicitly supplied). Inspect saved activity HR zones and comparable history. No live HR settings or streams are returned. detail=summary omits repeated instructions and full histories."""
    return call('prepare_analysis',{'as_of':as_of,'ftp':ftp,'target_id':target_id,'history_days':history_days,'refresh':refresh,'detail':detail})

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
def analyze(as_of: str, ftp: float | None = None, detail: str = "full") -> dict:
    """Review cached evidence; no network query. Use prepare_analysis before new training questions. Explicit indoor FTP overrides an eligible cached value."""
    return call('analyze',{'as_of':as_of,'ftp':ftp,'detail':detail})

@mcp.tool()
def propose_plan(as_of: str, ftp: float | None = None, detail: str = "full") -> dict:
    """Save next-day and seven-day DRAFT only. Use preview_icu for export; external execution requires independent user CLI."""
    return call('propose_plan',{'as_of':as_of,'ftp':ftp,'detail':detail})

@mcp.tool()
def list_proposals(limit: int | None = None, after: int | None = None, detail: str = "full") -> list | dict:
    """Read drafts, never execution state. Omit limit/after for legacy full list; use limit=20 and detail=summary for bounded pages, then get_proposal for a selected draft."""
    args={'detail':detail}
    if limit is not None: args['limit']=limit
    if after is not None: args['after']=after
    return call('list_proposals',args)

@mcp.tool()
def get_proposal(proposal_id: str, detail: str = "full") -> dict:
    """Read one draft by ID with provenance protection. Full details by default."""
    return call('get_proposal',{'proposal_id':proposal_id,'detail':detail})

@mcp.tool()
def preview_icu(proposal_id: str, account: str, timezone: str, ftp: float | None = None, selection: str = 'next') -> dict:
    """Read-only minimal export, never approval or execution. Save result JSON for independent user review."""
    return call('preview_icu', {'proposal_id':proposal_id,'account':account,'timezone':timezone,'ftp':ftp,'selection':selection})

def main(): mcp.run(transport='stdio')

if __name__=='__main__': main()
