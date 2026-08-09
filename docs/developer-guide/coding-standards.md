# Coding Standards

## Contents

Exception message conventions
Naming conventions
Property ordering
Public/private layout
Type hint expectations
Docstring style
Logging conventions

## Standards
- Python 3.11+
- requests.Session
- HTTPDigestAuth
- Fully typed
- Google-style docstrings
- Keyword-only constructor
- Immutable public interface
- Context manager support
- No hidden magic
- Minimal constructor validation + recorder verification
- SDK exceptions → sentence case with punctuation
- Built-in exceptions → follow the standard library style (lowercase, no period)
- Modules are named for the domain they own, not the caller that uses them

## Consistency Over Preference

When multiple reasonable implementations exist, the project values consistency over individual preference. Existing project conventions should be followed unless there is a compelling reason to change them.

## General Rule

Code is read far more often than it is written.
Optimize for readability and maintainability over brevity.