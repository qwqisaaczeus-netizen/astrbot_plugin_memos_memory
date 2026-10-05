"""Immutable handoff from the existing authoritative source archive."""
from dataclasses import asdict, dataclass
import hashlib
import math
from pathlib import Path

from .preflight import readonly
from .store import encoded


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Turn:
    id: int
    index: int
    role: str
    content: str
    event_ts: float
    timezone: str
    digest: str


@dataclass(frozen=True)
class SourceBatch:
    batch_id: str
    scope: str
    revision: str
    turns: tuple[Turn, ...]


def load_batch(path: Path, batch_id: str, scope: str) -> SourceBatch:
    db = readonly(Path(path))
    try:
        db.execute('BEGIN')
        batch = db.execute('SELECT session_id,message_count FROM source_batches WHERE batch_id=?', (batch_id,)).fetchone()
        if batch is None or batch['session_id'] != scope:
            raise SourceError('missing batch or scope mismatch')
        rows = db.execute('''SELECT id,turn_index,role,content,event_ts,event_timezone,content_hash
                             FROM source_turns WHERE batch_id=? ORDER BY turn_index''', (batch_id,)).fetchall()
        if not rows or len(rows) != batch['message_count']:
            raise SourceError('source manifest incomplete')
        turns = []
        for i, row in enumerate(rows):
            content = row['content']
            if row['turn_index'] != i or not isinstance(content, str):
                raise SourceError('source ordering/content invalid')
            digest = hashlib.sha256(content.encode()).hexdigest()
            if digest != row['content_hash']:
                raise SourceError('source hash mismatch')
            ts = float(row['event_ts'] or 0)
            if not math.isfinite(ts) or ts < 0:
                raise SourceError('invalid source time')
            turns.append(Turn(row['id'], i, row['role'], content, ts, row['event_timezone'] or '', digest))
        manifest = [{'id':t.id, 'index':t.index, 'role':t.role, 'event_ts':t.event_ts,
                     'timezone':t.timezone, 'digest':t.digest} for t in turns]
        revision = hashlib.sha256(encoded([scope,batch_id,manifest]).encode()).hexdigest()
        return SourceBatch(batch_id, scope, revision, tuple(turns))
    finally:
        db.close()


@dataclass(frozen=True)
class Span:
    turn_id: int
    start: int
    end: int

    @property
    def key(self):
        return f'{self.turn_id}:{self.start}:{self.end}'


@dataclass(frozen=True)
class Shard:
    owned: tuple[Span, ...]
    context: tuple[Span, ...]


def split_batch(batch: SourceBatch, char_limit=12000, max_shards=24) -> tuple[Shard, ...]:
    if type(char_limit) is not int or char_limit < 128 or not 1 <= max_shards <= 32:
        raise ValueError('invalid shard bounds')
    units = []
    i = 0
    while i < len(batch.turns):
        turn = batch.turns[i]
        # Keep a complete adjacent exchange together whenever capacity permits.
        if (turn.role == 'user' and i+1 < len(batch.turns) and batch.turns[i+1].role == 'assistant'
                and len(turn.content)+len(batch.turns[i+1].content) <= char_limit):
            units.append([Span(t.id,0,len(t.content)) for t in batch.turns[i:i+2]])
            i += 2
            continue
        text, start = turn.content, 0
        if not text:
            units.append([Span(turn.id,0,0)])
        while start < len(text):
            end = min(len(text), start+char_limit)
            if end < len(text):
                cut = max(text.rfind(sep,start+char_limit//2,end) for sep in ('\n','。','！','？','. '))
                if cut >= start+char_limit//2:
                    end = cut+1
            units.append([Span(turn.id,start,end)])
            start = end
        i += 1
    groups, current, size = [], [], 0
    for unit in units:
        length = sum(s.end-s.start for s in unit)
        if current and size+length > char_limit:
            groups.append(tuple(current))
            current, size = [], 0
        current.extend(unit)
        size += length
    if current:
        groups.append(tuple(current))
    if len(groups) > max_shards:
        raise SourceError('capacity requires explicit larger job; no truncation')
    result = []
    for index, owned in enumerate(groups):
        context = []
        if index:
            s=groups[index-1][-1]
            context.append(Span(s.turn_id,max(s.start,s.end-256),s.end))
        if index+1<len(groups):
            s=groups[index+1][0]
            context.append(Span(s.turn_id,s.start,min(s.end,s.start+256)))
        result.append(Shard(owned,tuple(context)))
    return tuple(result)


def shard_payload(batch: SourceBatch, shard: Shard) -> dict:
    turns = {t.id:t for t in batch.turns}
    def item(span):
        t=turns[span.turn_id]
        return {**asdict(span), 'span_id':span.key, 'role':t.role, 'event_ts':t.event_ts,
                'timezone':t.timezone, 'text':t.content[span.start:span.end]}
    return {'source_revision':batch.revision, 'owned':[item(s) for s in shard.owned],
            'context_only':[item(s) for s in shard.context], 'capacity_basis':'characters_not_tokenizer'}
