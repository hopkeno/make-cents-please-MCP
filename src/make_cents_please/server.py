"""Builds the MCP server and registers provider tools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import Config
from .providers import charityrecord, ynab

INSTRUCTIONS = """\
Access to the user's YNAB budget. Amounts are decimal strings in the plan's
currency; negative amounts are outflows. Tools default to the configured plan, so plan_id
is usually unnecessary. Find ids with ynab_list_accounts, ynab_list_categories,
ynab_list_payees or ynab_list_plans before calling get_* tools. Prefer filters (dates,
month, account, category) and small limits to keep responses short. The YNAB API allows
200 requests per hour, and responses are cached, so repeated questions are cheap.
"""

WRITE_INSTRUCTIONS = """
Write tools are enabled. Every write is two steps: call the tool without
confirmation_token to get a preview, show that preview to the user in plain language and
ask for approval, and only after the user explicitly approves call the same tool again with
the same arguments plus the confirmation_token. Never confirm on the user's behalf, and
never reuse a token for different arguments. Write tools always need plan_id and
plan_name, and every other id needs its matching *_name (exactly as the list tools show
it) so the user sees names when approving; the server rejects names that do not match.
"""

CHARITYRECORD_INSTRUCTIONS = """
Charity Record tools (charityrecord_*) work with CSV files in a local folder: exports the
user downloads from Charity Record, and import files written here for the user to upload.
Charity Record has no API, so nothing is sent to it directly. When recording donations,
use the values the user gives and never estimate fair market values. Writing import files
uses the same preview-then-confirm flow as other writes.
"""


def build_server(config: Config, client: ynab.YnabClient | None = None) -> MCPServer:
    ynab_tools = config.ynab_tools
    instructions = ""
    if ynab_tools:
        instructions += INSTRUCTIONS + (WRITE_INSTRUCTIONS if config.allow_writes else "")
    if config.charityrecord_dir:
        instructions += CHARITYRECORD_INSTRUCTIONS
    server = MCPServer(
        name="make-cents-please",
        version=__version__,
        instructions=instructions.strip(),
        log_level=config.log_level if config.log_level in _LEVELS else "WARNING",
    )
    # With YNAB_TOOLS=off the token is optional; without one there is no YNAB client and
    # the Charity Record tools that read YNAB are left out.
    if client is None and config.token is not None:
        client = ynab.YnabClient(config)
    if ynab_tools and client is not None:
        ynab.register(server, client)
        if config.allow_writes:
            ynab.register_write_tools(server, client)
    if config.charityrecord_dir:
        charityrecord.register(server, charityrecord.Folder(config.charityrecord_dir), client)
    return server


_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
