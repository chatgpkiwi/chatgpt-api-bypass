# chatgpt-api-bypass

Turn your Codex subscription into a small local, OpenAI-compatible API.

`codex-api.py` runs an HTTP listener that lets apps written for the ChatGPT API talk to your local Codex CLI instead.

Why?

The ChatGPT API costs money to use, whereas Codex limits are included in ChatGPT plus subscription. 
But... we may use a custom API that points to Codex instead of ChatGPT API. 

So is this just to save money?  
Yes. No. Yes. 

## How does it work

This has only been tested in Ubuntu Linux.

1. Install Codex CLI and authenticate.
2. Clone this project, edit `config.yaml`, then run `python3 codex-api.py`.
3. Point your OpenAI-compatible app at `http://127.0.0.1:8000/v1`.

That’s it. Run `codex-api-systemd install` if you want it as a systemd service.  
And then `systemctl --user start codex-api`

## Is it just like using ChatGPT API?

No. The Codex CLI injects a bit of tool-calling instruction bloat to your prompts. So it uses more tokens than a pure prompt. We stripped out as much of it as we could but at the end of the day, your prompts still get some extra text from Codex CLI, ~1k extra tokens per call.

## No encryption?

codex-api serves plain HTTP and defaults to localhost. If you expose it to a network, put a serious firewall app with whitelisting in front of it, like [proxyble](www.proxyble.com).

If using proxyble for HTTPS, you can also configure API keys in `config.yaml` for client authentication.

## Curious how it works?

[DESIGN.md](DESIGN.md) is the detailed feature inventory: App Server lifecycle, stateful Responses, prompt minimization, compaction, MCP isolation, upgrades, and operational rollback notes. It is the right place for Codex agents and maintainers; this README is the friendly front door.
