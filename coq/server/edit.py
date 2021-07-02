from dataclasses import dataclass
from itertools import chain, repeat
from pprint import pformat
from typing import AbstractSet, Iterator, MutableSequence, Sequence, Tuple

from pynvim import Nvim
from pynvim.api.buffer import Buffer
from pynvim_pp.api import buf_set_lines, cur_win, win_get_buf, win_set_cursor
from pynvim_pp.logging import log
from pynvim_pp.operators import operator_marks
from std2.itertools import deiter
from std2.types import never

from ..consts import DEBUG
from ..shared.trans import trans_adjusted
from ..shared.types import (
    UTF8,
    UTF16,
    ApplicableEdit,
    Context,
    ContextualEdit,
    Edit,
    NvimPos,
    RangeEdit,
    SnippetEdit,
)
from ..snippets.parse import parse
from .databases.buffers.database import BDB
from .nvim.completions import UserData
from .registrants.marks import mark
from .rt_types import Stack


@dataclass(frozen=True)
class _EditInstruction:
    primary: bool
    begin: NvimPos
    end: NvimPos
    cursor_yoffset: int
    cursor_xpos: int
    new_lines: Sequence[str]


@dataclass(frozen=True)
class _Lines:
    lines: Sequence[str]
    b_lines8: Sequence[bytes]
    b_lines16: Sequence[bytes]
    len8: Sequence[int]


def _lines(lines: Sequence[str]) -> _Lines:
    b_lines8 = tuple(line.encode(UTF8) for line in lines)
    return _Lines(
        lines=lines,
        b_lines8=b_lines8,
        b_lines16=tuple(line.encode(UTF16) for line in lines),
        len8=tuple(len(line) for line in b_lines8),
    )


def _rows_to_fetch(
    ctx: Context,
    edit: ApplicableEdit,
    *edits: ApplicableEdit,
) -> Tuple[int, int]:
    row, _ = ctx.position

    def cont() -> Iterator[int]:
        for e in chain((edit,), edits):
            if isinstance(e, ContextualEdit):
                lo = row - (len(e.old_prefix.split(ctx.linefeed)) - 1)
                hi = row + (len(e.old_suffix.split(ctx.linefeed)) - 1)
                yield from (lo, hi)

            elif isinstance(e, RangeEdit):
                (lo, _), (hi, _) = e.begin, e.end
                yield from (lo, hi)

            elif isinstance(e, Edit):
                yield row

            else:
                never(e)

    line_nums = tuple(cont())
    return min(line_nums), max(line_nums) + 1


def _contextual_edit_trans(
    ctx: Context, lines: _Lines, edit: ContextualEdit
) -> _EditInstruction:
    row, col = ctx.position
    prefix_lines = edit.old_prefix.split(ctx.linefeed)
    suffix_lines = edit.old_suffix.split(ctx.linefeed)

    r1 = row - (len(prefix_lines) - 1)
    r2 = row + (len(suffix_lines) - 1)

    c1 = (
        lines.len8[r1] - len(prefix_lines[0].encode(UTF8))
        if len(prefix_lines) > 1
        else col - len(prefix_lines[0].encode(UTF8))
    )
    c2 = (
        len(suffix_lines[-1].encode(UTF8))
        if len(prefix_lines) > 1
        else col + len(suffix_lines[0].encode(UTF8))
    )

    begin = r1, c1
    end = r2, c2

    new_lines = edit.new_text.split(ctx.linefeed)
    new_prefix_lines = edit.new_prefix.split(ctx.linefeed)
    cursor_yoffset = -len(prefix_lines) + len(new_prefix_lines)
    cursor_xpos = (
        len(new_prefix_lines[-1].encode(UTF8))
        if len(new_prefix_lines) > 1
        else len(ctx.line_before.encode(UTF8))
        - len(prefix_lines[-1].encode(UTF8))
        + len(new_prefix_lines[0].encode(UTF8))
    )

    inst = _EditInstruction(
        primary=True,
        begin=begin,
        end=end,
        cursor_yoffset=cursor_yoffset,
        cursor_xpos=cursor_xpos,
        new_lines=new_lines,
    )
    return inst


def _edit_trans(
    unifying_chars: AbstractSet[str],
    ctx: Context,
    lines: _Lines,
    edit: Edit,
) -> _EditInstruction:
    adjusted = trans_adjusted(unifying_chars, ctx=ctx, edit=edit)
    inst = _contextual_edit_trans(ctx, lines=lines, edit=adjusted)
    return inst


def _range_edit_trans(
    unifying_chars: AbstractSet[str],
    ctx: Context,
    primary: bool,
    lines: _Lines,
    edit: RangeEdit,
) -> _EditInstruction:
    new_lines = edit.new_text.split(ctx.linefeed)

    if primary and len(new_lines) <= 1 and edit.begin == edit.end:
        return _edit_trans(unifying_chars, ctx=ctx, lines=lines, edit=edit)
    else:
        (r1, ec1), (r2, ec2) = sorted((edit.begin, edit.end))

        if edit.encoding == UTF16:
            c1 = len(lines.b_lines16[r1][: ec1 * 2].decode(UTF16).encode(UTF8))
            c2 = len(lines.b_lines16[r2][: ec2 * 2].decode(UTF16).encode(UTF8))
        elif edit.encoding == UTF8:
            c1 = len(lines.b_lines8[r1][:ec1])
            c2 = len(lines.b_lines8[r2][:ec2])
        else:
            raise ValueError(f"Unknown encoding -- {edit.encoding}")

        begin = r1, c1
        end = r2, c2

        cursor_yoffset = (r2 - r1) + (len(new_lines) - 1)
        cursor_xpos = (
            (
                len(new_lines[-1].encode(UTF8))
                if len(new_lines) > 1
                else len(lines.b_lines8[r2][:c1]) + len(new_lines[0].encode(UTF8))
            )
            if primary
            else -1
        )

        inst = _EditInstruction(
            primary=primary,
            begin=begin,
            end=end,
            cursor_yoffset=cursor_yoffset,
            cursor_xpos=cursor_xpos,
            new_lines=new_lines,
        )
        return inst


def _consolidate(
    instruction: _EditInstruction, *instructions: _EditInstruction
) -> Sequence[_EditInstruction]:
    edits = sorted(chain((instruction,), instructions), key=lambda i: (i.begin, i.end))
    pivot = 0, 0
    stack: MutableSequence[_EditInstruction] = []

    for edit in edits:
        if edit.begin >= pivot:
            stack.append(edit)
            pivot = edit.end

        elif edit.primary:
            while stack:
                conflicting = stack.pop()
                if conflicting.end <= edit.begin:
                    break
            stack.append(edit)
            pivot = edit.end

        else:
            pass

    return stack


def _instructions(
    ctx: Context,
    unifying_chars: AbstractSet[str],
    lines: _Lines,
    primary: ApplicableEdit,
    secondary: Sequence[RangeEdit],
) -> Sequence[_EditInstruction]:
    def cont() -> Iterator[_EditInstruction]:
        if isinstance(primary, RangeEdit):
            yield _range_edit_trans(
                unifying_chars,
                ctx=ctx,
                primary=True,
                lines=lines,
                edit=primary,
            )

        elif isinstance(primary, ContextualEdit):
            yield _contextual_edit_trans(ctx, lines=lines, edit=primary)

        elif isinstance(primary, Edit):
            yield _edit_trans(unifying_chars, ctx=ctx, lines=lines, edit=primary)

        else:
            never(primary)

        for edit in secondary:
            yield _range_edit_trans(
                unifying_chars,
                ctx=ctx,
                primary=False,
                lines=lines,
                edit=edit,
            )

    instructions = _consolidate(*cont())
    return instructions


def _new_lines(
    lines: _Lines, instructions: Sequence[_EditInstruction]
) -> Sequence[str]:
    it = deiter(range(len(lines.b_lines8)))
    stack = [*reversed(instructions)]

    def cont() -> Iterator[str]:
        inst = None

        for idx in it:
            if stack and not inst:
                inst = stack.pop()

            if inst:
                (r1, c1), (r2, c2) = inst.begin, inst.end
                if idx >= r1 and idx <= r2:
                    it.push_back(idx)
                    lit = iter(inst.new_lines)

                    for idx, new_line in zip(it, lit):
                        if idx == r1 and idx == r2:
                            new_lines = tuple(lit)
                            if new_lines:
                                *body, tail = new_lines
                                yield lines.b_lines8[r1][:c1].decode(UTF8) + new_line
                                yield from body
                                yield tail + lines.b_lines8[r2][c2:].decode(UTF8)
                            else:
                                yield (
                                    lines.b_lines8[r1][:c1].decode(UTF8)
                                    + new_line
                                    + lines.b_lines8[r2][c2:].decode(UTF8)
                                )
                            break
                        elif idx == r1:
                            yield lines.b_lines8[r1][:c1].decode(UTF8) + new_line
                        elif idx == r2:
                            new_lines = tuple(lit)
                            if new_lines:
                                *body, tail = new_lines
                                yield new_line
                                yield from body
                                yield tail + lines.b_lines8[r2][c2:].decode(UTF8)
                            else:
                                yield new_line + lines.b_lines8[r2][c2:].decode(UTF8)
                            break
                        else:
                            yield new_line
                    inst = None
                else:
                    yield lines.lines[idx]
            else:
                yield lines.lines[idx]

    return tuple(cont())


def _cursor(cursor: NvimPos, instructions: Sequence[_EditInstruction]) -> NvimPos:
    row, _ = cursor
    col = -1

    for inst in instructions:
        row += inst.cursor_yoffset
        col = inst.cursor_xpos
        if inst.primary:
            break

    assert col != -1
    return row, col


def _visual(nvim: Nvim, buf: Buffer, context: Context, db: BDB) -> str:
    (row1, col1), (row2, col2) = operator_marks(nvim, buf=buf, visual_type=None)
    _, lines = db.lines(context.buf_id, lo=row1, hi=row2 + 1)
    if not lines:
        return ""
    elif len(lines) == 1:
        return lines[0].encode()[col1 : col2 + 1].decode()
    else:
        head = lines[0].encode()[col1:].decode()
        body = lines[1:-1]
        tail = lines[-1].encode()[: col2 + 1].decode()
        return context.linefeed.join(chain((head,), body, (tail,)))


def edit(nvim: Nvim, stack: Stack, context: Context, data: UserData) -> Tuple[int, int]:
    cursor = context.position

    win = cur_win(nvim)
    buf = win_get_buf(nvim, win=win)

    primary, marks = (
        parse(
            stack.settings.match.unifying_chars,
            context=context,
            snippet=data.primary_edit,
            sort_by=data.sort_by,
            visual=_visual(nvim, buf=buf, context=context, db=stack.bdb),
        )
        if isinstance(data.primary_edit, SnippetEdit)
        else (data.primary_edit, ())
    )
    lo, hi = _rows_to_fetch(
        context,
        primary,
        *data.secondary_edits,
    )
    _, limited_lines = stack.bdb.lines(context.buf_id, lo=lo, hi=hi)
    lines = tuple(chain(repeat("", times=lo), limited_lines))
    view = _lines(lines)

    instructions = _instructions(
        context,
        unifying_chars=stack.settings.match.unifying_chars,
        lines=view,
        primary=primary,
        secondary=data.secondary_edits,
    )
    new_lines = _new_lines(view, instructions=instructions)
    n_row, n_col = _cursor(cursor, instructions=instructions)

    if DEBUG:
        msg = pformat((data, instructions, (n_row + 1, n_col + 1), new_lines))
        log.debug("%s", msg)

    buf_set_lines(nvim, buf=buf, lo=lo, hi=hi, lines=new_lines[lo:])
    win_set_cursor(nvim, win=win, row=n_row, col=n_col)

    stack.idb.inserted(data.sort_by or primary.new_text)

    if marks:
        mark(nvim, settings=stack.settings, buf=buf, marks=marks)

    return n_row, n_col

