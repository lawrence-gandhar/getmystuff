"""
What can go wrong making a file for somebody, as two exceptions.

Fewer levels than ``email_dispatch/errors.py`` has, and the reason is the whole shape of
this module: **there is no queue and no second party.** An email is rendered here, handed
to a relay somewhere else, and may fail hours later in a way nothing on this side can
predict — hence three failures and a ``retryable`` flag. A file is written by this process
onto this disk, synchronously, and either exists when the block finishes or does not.

So there are two failures, split by whose problem it is:

*there is nothing to write* — :class:`SourceError`
    The block's data source does not resolve to rows: a variable holding prose asked to
    become a spreadsheet, a Run Graph block that has not run yet, a result larger than
    :data:`file_writer.FILE_MAX_ROWS`. Knowable before a byte is written, and **nothing is
    written** — a partial file is worse than none, because a partial file gets downloaded.

*the file could not be written* — :class:`WriteError`
    The rows were fine and the disk, the format library or the path was not. Rarer and
    less actionable, but kept separate because the two need different sentences: the first
    says "point this block at something that produces rows", the second says "this could
    not be written" and puts the detail in the log.

Both are refusals a canvas can route: the flow engine takes the block's ``error`` port
(and, failing that, the enclosing call's ``failed`` port), and the graph runner turns them
into a ``NodeFailure``. Neither is ever swallowed into an empty file — a Create File block
that quietly produced a header row and nothing else is how somebody emails a customer an
empty spreadsheet.

**Every message is written for the operator who drew the block**, naming the block and
what to do about it, in the register ``IntegrationFailure`` and ``ToolQueryError`` use.
Nothing here reaches a visitor: the flow engine says only that something went wrong, and
the sentence goes to the log and to the operator's canvas.
"""


class FileFailure(Exception):
    """
    Base for everything in this module.

    Not raised directly. It exists so the two canvas adapters can write one ``except``
    — both of them genuinely do not care which kind it was, because both do the same
    thing with it: take the failure port and log the sentence.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SourceError(FileFailure):
    """
    The block's data does not resolve to rows, so there is nothing to write.

    ``block`` is the label or id of the block whose data was being read — usually the
    Create File block itself, but the *named* block when a flow's Create File points at a
    Run Graph or AI Fallback block that has not produced anything. Carried separately from
    the sentence so a property panel can highlight the right box rather than showing a
    banner about the canvas as a whole.
    """

    def __init__(self, message: str, *, block: str = "") -> None:
        super().__init__(message)
        self.block = block


class WriteError(FileFailure):
    """
    The rows were fine; putting them on disk was not.

    A full disk, a format library refusing a value it cannot represent, a path that could
    not be created. The message is deliberately plain — there is nothing an operator can
    configure their way out of here, so it says what failed and leaves the diagnosis to
    the log, which has the traceback.
    """
