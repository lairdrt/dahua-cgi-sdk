# dahua-rpc-sdk

`dahua-rpc-sdk` is a synchronous Python SDK for working with Dahua-family
network video recorders through their RPC2 and RTSP interfaces. It presents
recorder concepts—cameras, stream profiles, indexed recordings, snapshots,
and playback sessions—as typed Python objects instead of exposing firmware
method names and response dictionaries to applications.

The project is particularly concerned with historical media. It can search the
recorder's native media index, preserve recorder-local timezone semantics,
open stateful RTSP playback for an indexed recording, and expose encoded
RTP/RTCP packets without downloading a DAV file or decoding or transcoding the
media. Development and live characterization use a Lorex-branded recorder that
exposes Dahua-compatible RPC2 and RTSP behavior; those observations are useful
engineering evidence, not a promise that every Dahua-family device behaves
identically.

## Why this project exists

Dahua-family recorders offer useful capabilities through their own desktop and
mobile applications, but those capabilities are awkward to integrate into
other systems. Media search, recorder-local timestamps, playback control, and
camera configuration span several protocol surfaces and often leak
firmware-specific details into application code.

This SDK provides a maintainable boundary around those details. The public API
models the recorder rather than the wire protocol, retains native recorder
semantics, and keeps encoded media encoded. An application can decide how to
present or distribute media without first converting it into frames or
discarding RTP/RTCP timing information.

## Current capabilities

The production package currently implements:

- authenticated RPC2 sessions and recorder identity discovery;
- recorder IANA timezone discovery and aware current time;
- 1-based camera inventory, connection state, and Encode stream profiles;
- direct RTSP live-video sessions for Main or Extra1 profiles;
- lazy indexed recording and snapshot searches;
- immutable `Recording`, `Snapshot`, `Camera`, and `StreamProfile` models;
- explicit stored DAV and JPEG export for indexed media;
- file-specific recorded RTSP playback with pause, resume, and seek;
- optional recorded audio SETUP in the same RTSP session as video;
- generic SDP `MediaTrack` metadata for discovered video and audio;
- ordered, encoded video/audio RTP and RTCP access; and
- RTP header and RTCP Sender Report metadata.

Construction of `DahuaClient` verifies connectivity, authentication, recorder
identity, and timezone. The client is a context manager and closes the RPC
session plus any live or recorded RTSP sessions it owns.

## Quick example

This example searches the last hour of channel 1, starts the first indexed
recording with audio requested, and observes encoded packets. It does not
download or decode the recording.

```python
from datetime import timedelta

from dahua_rpc import DahuaClient


with DahuaClient(
    host="192.0.2.10",
    username="sdk-user",
    password="replace-me",
) as client:
    end = client.current_time
    start = end - timedelta(hours=1)

    with client.media.recordings(channel=1, start=start, end=end) as results:
        recording = next(results, None)

    if recording is not None:
        with client.media.playback(recording, audio=True) as playback:
            playback.start()
            packets = playback.receive_packets(duration=1.0)

            for packet in packets:
                print(
                    packet.media_type,
                    packet.packet_type,
                    packet.rtp_timestamp,
                    packet.ssrc,
                )
```

`audio=True` is an explicit capability request. If the recording's SDP does
not contain a usable audio track, playback startup raises an SDK error rather
than inventing or silently simulating audio.

## Architecture

```text
Dahua/Lorex recorder
    |
    +-- RPC2
    |     identity, time and timezone
    |     camera inventory and Encode metadata
    |     indexed recordings and snapshots
    |
    +-- RTSP
          live streams
          stateful recorded playback
          encoded video/audio RTP and RTCP
                    |
                    v
             dahua-rpc-sdk
                    |
                    v
          application/controller
```

RPC2 media indexing and RTSP playback are related but separate concerns. A
`Recording` is immutable metadata returned by the recorder's index. Passing it
to `client.media.playback()` creates a `RecordingPlayback` bound to that one
indexed recorder file and owning one authenticated RTSP session.

This separation matters at file boundaries: the SDK does not pretend that two
DAV records are one recorder-side stream. A higher-level controller can prepare
the next playback session and decide how to present continuity to consumers.

The broader domain philosophy is documented in
[`ARCHITECTURE.md`](ARCHITECTURE.md). Some older aspirational examples in the
architecture documents describe future domain areas; current source and tests
remain authoritative for implemented APIs.

## Historical media and timezone correctness

Dahua media-search timestamps are offset-free recorder-local wall-clock
values. Treating them as UTC or attaching the host computer's current offset
would corrupt historical results, particularly across daylight-saving
transitions.

The SDK therefore:

1. discovers the recorder's configured IANA timezone from RPC configuration;
2. requires timezone-aware public search bounds;
3. converts bounds from any timezone to recorder-local wall time;
4. returns aware recording and snapshot datetimes in `client.timezone`; and
5. uses `zoneinfo` rules, including historical DST changes.

The recorder wire format cannot encode `datetime.fold`. During the repeated
fall-back hour, two distinct instants have the same wall-clock representation.
The SDK rejects such ambiguous search bounds instead of silently selecting one.
It likewise rejects ambiguous or nonexistent recorder times returned around DST
transitions. This is deliberate fail-safe behavior.

## Recorded playback model

The relationship is intentionally direct:

```text
Recording  ->  RecordingPlayback  ->  one recorder RTSP session
```

`RecordingPlayback` is stateful and supports:

- `start()`;
- `pause()` and `resume()`;
- `seek(seconds)` and `seek_relative(delta_seconds)`;
- `receive(duration)` for legacy video RTP receipt statistics;
- `receive_packets(duration)` for encoded media; and
- idempotent `close()` and context-manager cleanup.

`position` is a recording-relative NPT estimate anchored by recorder-confirmed
RTSP ranges. A seek cannot cross the indexed file's duration. Transparent
multi-file playback, downstream source continuity, and DAV-boundary handoff do
not belong to `RecordingPlayback` itself.

## Encoded media access

`receive_packets()` exists for applications that need to preserve transport
media rather than turn it into decoded frames. It returns ordered immutable
`EncodedMediaPacket` objects containing:

- video or audio identity;
- RTP or RTCP identity;
- the complete original RTP or compound RTCP packet bytes;
- the upstream interleaved channel and monotonic arrival time;
- RTP payload type, marker, sequence number, timestamp, and SSRC; and
- parsed RTCP packet types and Sender Report SSRC/NTP/RTP fields.

The SDK does not rewrite these domains. Seeking within a recording or opening
another indexed file may produce recorder-side timestamp, sequence, or SSRC
changes; a presentation bridge is responsible for downstream continuity.

A `RecordingPlayback` is the sole reader of its recorder socket. Applications
must not call `receive()` and `receive_packets()` concurrently or have several
consumers read independently. Local fan-out belongs above one receive loop.

## Audio

Recorded playback remains video-only by default for backward compatibility:

```python
client.media.playback(recording)
```

Request discovered audio explicitly with:

```python
client.media.playback(recording, audio=True)
```

When available, video and audio are SETUP as separate tracks in one aggregate
RTSP session. They retain independent RTP sequence, timestamp, and SSRC domains.
RTCP Sender Reports can give a higher layer a common NTP timing reference. The
SDK exposes this information but does not decode, mix, delay, or synchronize
audio. AAC was observed on the reference installation; it is not assumed to be
universal.

## Main, Extra1, and codecs

Profile identity is not codec identity. Main and Extra1 are configurable
recorder stream profiles, not synonyms for H.265 and H.264.

The reference installation currently uses H.265 for Main and H.264 for Extra1,
but either profile may be configured differently. The SDK reads Encode metadata
and recorded SDP rather than embedding that installation policy. Applications
choosing between quality, bandwidth, and client compatibility should inspect
the actual `StreamProfile` and `MediaTrack` values.

## Explicit export versus playback

The SDK offers two distinct historical-media paths:

- `recording_bytes()` and `snapshot_bytes()` explicitly export indexed DAV or
  JPEG content through the recorder's `RPC_Loadfile` endpoint; and
- `playback()` opens stateful RTSP media directly from indexed metadata.

Recorded playback and `receive_packets()` do not download a DAV file and do
not use CGI-style media search or snapshot workarounds. The project does use
HTTP for RPC2 and retains explicit `RPC_Loadfile` export where the caller
specifically asks for stored bytes. This distinction is intentional.

## What the SDK deliberately does not do

The production API currently provides no:

- video or audio decoding;
- transcoding or codec conversion;
- HLS, WebRTC, or browser playback generation;
- Home Assistant UI or media browser;
- transparent playback across indexed DAV boundaries;
- downstream RTP sequence/timestamp/SSRC rewriting;
- production local RTSP presentation server; or
- automatic multi-consumer packet fan-out.

A loopback RTSP bridge exists under `tools/` as an architecture-validation
prototype. It is not a supported public SDK API.

## Findings from the reference recorder

Live probes against the Lorex/Dahua-compatible reference recorder have
observed the following. These are device-specific measurements, not general
Dahua guarantees:

- two H.265 Main sessions delivered concurrently for 30 seconds, and four Main
  sessions were also opened, repositioned, and received concurrently;
- concurrent Extra1 playback and seek worked for two cameras;
- absolute seek repositioned an existing file-specific RTSP session;
- adjacent DAV files created independent RTP source domains;
- a prepared `start -> seek -> pause -> resume` session reduced measured
  handoff interruption from roughly half a second to roughly one tenth of a
  second in the tested cases;
- tested adjacent Main and Extra1 files had compatible SDP codec
  initialization, though applications must still detect changes;
- video and audio emitted RTCP Sender Reports with useful RTP/NTP mappings; and
- the loopback bridge prototype forwarded H.265 video and AAC audio through one
  downstream RTSP connection across an upstream seek while keeping rewritten
  downstream RTP identities stable.

These probes measure packet receipt, not decoded picture quality, lip sync, or
browser compatibility. Longer-duration A/V synchronization and end-to-end Home
Assistant playback remain unverified.

## Reference environment

Validation has been performed against a Lorex-branded NVR exposing
Dahua-compatible RPC2, Digest-authenticated RTSP, indexed DAV media, Main and
Extra1 profiles, and attached IP cameras. The exact recorder model is not
recorded in the repository as a verified compatibility target, so this README
does not claim a model support matrix.

Compatibility should be established against each target recorder. Firmware,
OEM branding, enabled services, camera configuration, and codecs can all affect
behavior.

## Design principles

### Discover, don't assume

Camera slots, configured state, stream profiles, codecs, audio availability,
and SDP controls come from recorder responses.

### Preserve recorder semantics

The SDK does not fabricate capabilities, timestamps, media configuration, or
cross-file continuity.

### Keep encoded media encoded

RTP/RTCP bytes and timing remain available until an application deliberately
chooses a presentation or decoding layer.

### Fail safely

Malformed protocol data, unavailable requested audio, and ambiguous local
times produce explicit errors instead of guessed results.

### Separate transport from presentation

The SDK models recorder capabilities. UI, browser delivery, Home Assistant
presentation, and downstream continuity are higher-layer concerns.

### Maintain timing information

Audio/video RTP and RTCP timing is preserved for downstream decisions. A
single measured startup difference must never become a hard-coded delay.

## Installation

The project requires Python 3.11 or newer. Runtime dependencies are
`requests>=2.32.0` and `tzdata>=2025.2`.

No PyPI release workflow is documented yet. Install from a checkout:

```console
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -e .
```

Use recorder credentials with only the access needed by your application.

## Development

The repository does not currently declare a development dependency group, so
make `pytest` and `ruff` available in the development environment, then run:

```console
python -m pytest
python -m ruff check src tests
git diff --check
```

Unit tests use synthetic RPC, SDP, RTP, and RTCP fixtures and do not require
live hardware. Live probes are separate and opt-in.

## Development approach

The principal development toolset is Python in a project virtual environment
(`venv`), with pytest for automated tests, Ruff for lint and static checks, Git
and GitHub for source control, and Visual Studio Code for repository work. The
AI-assisted development environment used while this README was written includes
ChatGPT — GPT-5.6 Sol and OpenAI Codex — GPT-5.6 Codex; these tools and model
versions describe the engineering process, not project dependencies.

Engineering combines unit tests and synthetic protocol/media fixtures with
opt-in probes against reference hardware. Disposable characterization tools
under `tools/` test important assumptions before they enter production code,
including timezone behavior, media search, concurrent and packet-preserving
playback, DAV boundaries, Main/Extra1 profiles, RTP/RTCP, recorded audio, and
the local RTSP bridge. Changes are checked with pytest, Ruff, other focused
static validation, and Git diff/working-tree review. Architecture is refined
iteratively from those results rather than assumed in advance.

The human engineering role provides project goals and requirements,
operational experience, system-level judgment, architecture review and
approval, reference hardware and configuration, hands-on live testing,
interpretation of desired user behavior, review of tradeoffs and results, and
the final engineering and product decisions. ChatGPT GPT-5.6 Sol serves
primarily as an architecture and engineering collaborator: refining
requirements, identifying uncertainties and tradeoffs, designing and
interpreting focused experiments, planning implementation increments,
structuring documentation, and preparing scoped research or implementation
tasks. OpenAI Codex GPT-5.6 Codex serves primarily as the repository-level
implementation and verification agent: inspecting the actual checkout,
implementing approved changes, writing Python and tests, running validation,
constructing diagnostic probes, performing directed live checks, and reporting
changed files, evidence, and remaining uncertainty.

```text
human requirements and engineering judgment
                    |
                    v
architecture and investigation (ChatGPT GPT-5.6 Sol)
                    |
                    v
scoped implementation and probes (OpenAI Codex GPT-5.6 Codex)
                    |
                    v
automated tests and live evidence
                    |
                    v
human review and decision -> next iteration
```

This is an iterative, human-reviewed engineering loop, not autonomous code
generation. The project owner does not claim deep Python specialization;
confidence is instead built through system architecture review, automated
tests, focused live-hardware experiments, and repeated validation. AI-assisted
work is not presumed correct, and architectural and product decisions remain
subject to human review and approval.

## Diagnostics and research tools

`tools/` contains focused live-hardware experiments rather than stable public
APIs. They cover:

- multi-camera playback concurrency and session scaling;
- adjacent DAV boundaries and prepared sessions;
- SDP, codec, RTP, audio, and RTCP characterization;
- validation of the encoded-packet API; and
- a one-camera loopback RTSP bridge prototype.

Probe credentials come from environment variables. Review a tool's scope and
output policy before running it against a recorder; some older utilities also
exercise explicit stored-media export.

## Relationship to Home Assistant

`dahua-rpc-sdk` is the recorder, protocol, and encoded-media library.
`dahua-rpc-ha` is a separate Home Assistant integration built on the SDK. The
repositories intentionally have different responsibilities.

Current research is exploring controlled historical playback through a stable
local source owned by the SDK/controller. Local presentation, multi-consumer
fan-out, and Home Assistant Stream/HLS validation are not production features
of this package yet.

## Engineering direction

Likely next layers include encoded-media fan-out, productionizing the local
RTSP presentation boundary, RTP/RTCP continuity across seek and file changes,
multi-camera historical playback coordination, and Home Assistant
presentation. These are engineering directions, not release commitments.

## Security

- Never log recorder passwords, Digest response values, or session IDs.
- Treat indexed recorder file paths and camera metadata as sensitive.
- Keep experimental presentation bridges bound to loopback unless exposure is
  explicitly required and secured.
- Avoid embedding credentials in downstream URLs or attributes.
- Minimize recorder and network exposure for each consumer.

Camera inventory deliberately excludes recorder-stored camera credentials.
The SDK does not, however, turn an untrusted network into a trusted one;
deployment security remains the application's responsibility.

## Project status

The package is version `0.1.0` and under active engineering development. Its
implemented APIs are covered by unit tests and selected paths have been tested
against reference hardware, but there is no broad device compatibility matrix
or production-readiness claim. Public APIs may continue to evolve as recorder
variants and higher-level integration requirements are validated.
