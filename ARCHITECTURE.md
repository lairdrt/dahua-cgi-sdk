# System Architecture ("How is this project designed?")

This document is the authoritative description of the software architecture. Implementation decisions should
conform to this document unless modified through the project's architectural decision process.

## Project Vision

Design and implement a Python software development kit (SDK) that models 
[Dahua-based](https://www.dahuasecurity.com/products/network-products/network-recorders) network video recorders (NVRs)
naturally and eventually covers most meaningful functionality.

Produce an SDK exposing an interface to access and control NVRs to include managing live video streams, recorded events,
and overall device operation.

## Design Philosophy

The system architecture is organized around the Dahua NVR.

This SDK is designed for software engineers first and protocol experts second.

Meaning...

Someone should be productive without reading Dahua documentation.

## Architectural Principles

1.  If we cannot explain a design decision in ARCHITECTURE.md, we probably don't understand it well enough to implement it.
2.  A DahuaClient instance represents a connected recorder, not merely a communication channel.
3.  The SDK models the recorder as a domain, not the CGI protocol.
4.  A class exists only if it models a real concept in the recorder or significantly simplifies the public API.
5.  Resources expose operations that naturally belong to them.
6.  Prefer native Python idioms unless a custom abstraction provides significant additional value.
7.  Avoid creating abstractions until they provide clear, demonstrable value over native Python constructs.
8.  Don't design for the feature you might need. Design for the feature you have.
9.  Every class you don't write is a class you never have to maintain.
10. Public objects should satisfy their documented invariants immediately after successful construction.

### Hide implementation details behind an intuitive, stable public API.

Example:

Users should never know about:

`factory.create`
`findNextFile`
CGI query parameters
server-side search objects

They should know about:

`client.media.search(...)`

### Public APIs return domain objects, not protocol data.

Never:

`dict`

Always:

`Recording`
`Camera`
`Disk`
`Event`

### The public API should read like Python, not like firmware documentation.

Instead of:

`findNextFile(...)`

We want:

`for recording in client.media.search(...):`
`    ...`

### Abide by the Rule of Three:

Don't generalize until you've seen something three times.

For example:

- We won't build a generic event framework after one event type.
- We won't build a generic download framework after one download.
- We won't invent a base class because two classes look similar.

We'll wait until the pattern is undeniable. That tends to keep architectures lean.

### Architecture changes require one of three reasons:

- We discovered the current design cannot support a required capability.
- The new design is objectively simpler (fewer classes, fewer responsibilities, less coupling).
- The implementation exposes an architectural flaw that wasn't visible during design.

### Implementation must conform to the architecture.

The architecture should not drift to match the implementation.

## Layered Architecture

## Public API Philosophy

## Package Organization

## Testing Strategy

## Documentation Strategy

## Versioning Strategy

## Architectural Evolution
