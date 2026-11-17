# KoreComms

KoreComms is the external communications subsystem. It owns the integration surface for inbound and outbound messaging channels and bridges those conversations into the local KoreChat and KoreAgent workflow.

## Why it exists

The agent should not talk directly to Gmail, Discord, or other external systems. KoreComms isolates that risk, keeps credentials and adapter logic in one place, and converts channel activity into local suite events.

## What it does

- Polls or receives messages from supported interfaces
- Stores local conversation metadata and interface state
- Bridges inbound messages into KoreChat-backed agent conversations
- Delivers outbound messages once the agent marks them ready
- Provides browser pages for conversations, connections, compose, and activity

## Role in the suite

KoreComms (port 29609) is the only service that talks to Discord, Gmail, SFTP, and similar channels. Inbound traffic becomes KoreChat conversations and events that KoreAgent processes; replies the agent marks ready are delivered back out. KoreStack starts it with the rest of the suite.

## Architecture

| Element | Role |
|---|---|
| `interfaces/` | One adapter per channel (manual, Discord, Gmail, SFTP file) |
| Poller and queue manager | Background polling and ordered delivery |
| Database and crypto | Local interface state, conversation metadata, encrypted credentials |
| KoreChat client | Bridges messages into the canonical thread record |
| Browser pages | Conversations, connections, compose, and activity |

## Service contract

KoreComms owns transport adapters and delivery state, while KoreChat remains the canonical thread record.

- inbound channel traffic is translated into local conversations and events
- outbound delivery happens only after the agent writes a draft and marks it ready
- browser pages exist for operators to inspect conversations, configure interfaces, and inject manual messages

### Email message contracts

Classic Email and Gmail use versioned JSON Schema contracts shared by their adapters and the KoreComms poller:

- [`schemas/email.received.schema.json`](schemas/email.received.schema.json) describes the `email.received` event. Its `source` identifies the connection; `message` includes sender, From/Reply-To, To/Cc, subject, normalized body, MIME content type, reply references, and a small allow-list of useful headers.
- [`schemas/email.send.schema.json`](schemas/email.send.schema.json) describes the `email.send` command used by email adapters, including recipient, subject, content, content type, and optional reply/thread references.

The adapters emit or consume these contracts, and KoreComms validates them at the inbound poller and outbound email adapter boundaries. The public `POST /api/send` request remains backward-compatible (`interface_id`, `recipient` or `distribution_list_id`, `subject`, and `content`); KoreComms resolves distribution-list recipients and maps each delivery to an `email.send` command. The message body is normalized to plain text on receipt; `content_type` records the selected MIME body type. Raw, unfiltered message headers are not exposed.


## Install and configuration

- KoreComms reads host, port, and related service URLs from `config/korestack_config.json`
- Interface-specific credentials are configured through the subsystem itself and stored locally
- KoreComms depends on KoreChat for canonical thread history and event coordination

## New user notes

- Start with the manual interface first before wiring external providers
- Add one connection at a time and confirm send and receive flows before enabling more channels
- Treat external secrets and OAuth credentials as local environment state, not repository defaults
- An SFTP file connection is output-only: every delivery replaces its configured absolute remote file.
  Its server host key must be present in the service account's `known_hosts` file.

## Troubleshooting

| Problem | What to check |
|---|---|
| Connection polling appears idle | Confirm the interface is configured, enabled, and has valid credentials |
| Replies do not get delivered | Check KoreChat connectivity and whether outbound events are being produced |
| UI works but external channel traffic does not | Verify network access, credentials, and channel identifiers for that interface |
| Service starts but cannot bind | Confirm `services.korecomms.port` is free |

