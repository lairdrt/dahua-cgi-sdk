# Major Domain Concepts and their Relationships

# Simplified Domain Model

- Recorder: represented by DahuaClient
- Resources: entities managed by or attached to the recorder.
- Services: operations spanning multiple resources or recorder-wide capabilities.
- Value Objects: immutable objects representing values without identity.

# Represented as an Object

Recorder (DahuaClient)
|
|--- Camera (0..N)
|     |--- Stream
|     |--- PTZ
|
|--- Recording (queried via Media)
|--- Snapshot (queried via Media)
|
|--- Disk (0..N)
|
|--- User (0..N)
|
|--- Configuration
