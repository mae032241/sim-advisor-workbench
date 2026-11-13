# Simulation workbench bridge for DSH

Native Host bundle for the locally inspected DSH 0.2.2-rc.1 API. No dependencies,
build step, credential files, model launch, or changes to the DSH profile are
performed by the plugin. Install the directory through the official
`plugin_manager` / `install_bundle` operation.

In the intended DSH conversation, call `bind_workbench` with the full local
workbench URL, for example `http://127.0.0.1:8765/?session=<32-hex-plan-id>`.
The workbench must already be running. Registration uses the DSH Host PID,
`backend: dsh`, and this Agent's ID as `conversation_id`. The plan ID is separate.
The registration token stays in Host memory and is never returned to the model.

Only this Agent receives the server's current bound-tool catalog and returned
instructions. The bridge requests `/api/tools?bound=true` and uses those exact
descriptions and schemas, including `get_tool_contract` and future tools. This
catalog excludes external mailbox administration and session/token arguments.
Older servers without a bound catalog use the legacy tool allowlist. Tool
contracts can be queried by input-data JSON pointer and paged with
`offset`/`length`; contract lookup does not read source text or grant evidence.
Arguments cannot override the bound plan. The tools use the
authenticated `/external/tool` endpoint, retaining backend/model/conversation
provenance. Saving a plan uses current revision and the existing backend rules.

A 2-second inbox poll continues while the model runs. Idle messages use
`agent.followup`, running messages use `agent.steer`, and stop requests call
`agent.cancel({kind: 'user'})`. Disconnect removes the binding and its planning
tools, prompt, event listeners, and timer, keeping the DSH conversation/process.
The two local bind/unbind commands remain available until plugin/Agent disposal.

Only a successful native `session/event` `turn/end` (`reason.kind=completed`)
triggers a reply. The bridge keeps only the latest assistant message in that
turn, then sends its visible text once. Intermediate messages, tool-call
content, reasoning and token streams are not mirrored. An empty, interrupted,
reasoning-only or tool-call latest message is never replaced by an earlier
progress message. Canceled/failed/incomplete turns do not publish a final reply.
Turn boundaries are independent of Agent idle, so queued turns remain distinct.
Native status, heartbeat, necessary errors and interrupt/disconnect controls
remain lightweight. Display text beyond 15,900 characters is explicitly marked
as truncated; the complete DSH answer remains in DSH.

`unbind_workbench` detaches manually. Plugin unload and Agent disposal unregister
their bindings; temporary HTTP failure removes local integration and lets the
remote heartbeat expire if unregister could not be confirmed. Writes are never
automatically retried. The original SVG icon is authored for this bundle.

Validation: `node --test test/*.test.js` and `node --check index.js`.
Native source references used: `dsh-tools/lib/index.js` register/execute contract;
`dsh-system-prompt/lib/index.js` scoped section; `dsh-agent/lib/index.js` registry;
`dsh-api-session-controller/lib/index.js` prompt and stream/status listeners;
`dsh-llm/lib/index.js` user messages and visible text chunk shapes.
