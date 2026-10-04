"""windvane: keeps the state and steers a Claude Code session.

The engine behind the windvane plugin. The plugin's hooks module (TypeScript,
``hooks/``) runs this package for the work that reads and writes the store:
the hook events (``windvane.hooks``), the five plugin tools (``windvane.tools``),
the compaction brief (``windvane.brief``), the drafted checkpoint
(``windvane.draft``) and the daemon that serves them warm (``windvane.daemon``).
Standard library only; the semantic extra adds the encoder.
"""

__version__ = "0.1.0"
