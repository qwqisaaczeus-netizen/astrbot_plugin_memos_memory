"""Resolve typography and ordered sentence excerpts back to exact source slices."""
import re

from .sources import SourceError


def folded(text):
    result=[]; offsets=[]; i=0
    quotes={'“':'"','”':'"','‘':"'",'’':"'"}
    while i<len(text):
        char=text[i]
        if char=='\\' and i+1<len(text) and text[i+1] in ('"',"'"):
            result.append(text[i+1]);offsets.append(i+1);i+=2;continue
        if char.isspace():
            end=i+1
            while end<len(text) and text[end].isspace():end+=1
            # Keep Latin token boundaries: "not able" must not become "notable".
            if (i and end<len(text) and text[i-1].isascii() and text[i-1].isalnum()
                    and text[end].isascii() and text[end].isalnum()):
                result.append(' ');offsets.append(i)
            i=end;continue
        result.append(quotes.get(char,char));offsets.append(i);i+=1
    return ''.join(result),offsets


def excerpts(quote):
    chunks=[]; pending=''; start=0
    for match in re.finditer(r'[。！？；]|…{2,}',quote):
        pending+=quote[start:match.end()];start=match.end()
        if len(pending)>=8:chunks.append(pending);pending=''
    pending+=quote[start:]
    if pending:
        if len(pending)<8 and chunks:chunks[-1]+=pending
        else:chunks.append(pending)
    return chunks


def resolve(source,start,end,quote,before='',after=''):
    """No edit-distance match. Every returned character comes from this owned span."""
    segment=source[start:end]
    normalized,positions=folded(segment)
    target,_=folded(quote)
    left,_=folded(before);right,_=folded(after)
    if not target:raise SourceError('empty normalized quotation')

    def locations(piece):
        found=[];pos=0
        while True:
            pos=normalized.find(piece,pos)
            if pos<0:break
            found.append((pos,pos+len(piece)))
            if len(found)>16:raise SourceError('quotation too ambiguous')
            pos+=1
        return found

    def bounded(chain):
        a,b=chain[0][0],chain[-1][1]
        return (a>=len(left) and normalized[a-len(left):a]==left
                and normalized[b:b+len(right)]==right
                and positions[b-1]+1-positions[a]<=1200)

    direct=[[(a,b)] for a,b in locations(target) if bounded([(a,b)])]
    if len(direct)>1:raise SourceError('quotation missing or ambiguous; source retained')
    chains=direct
    if not chains:
        pieces=excerpts(target)
        if not 2<=len(pieces)<=8 or any(len(p)<8 for p in pieces):
            raise SourceError('quotation missing or ambiguous; source retained')
        chains=[[]]
        for piece in pieces:
            candidates=locations(piece)
            chains=[chain+[(a,b)] for chain in chains for a,b in candidates
                    if not chain or (a>=chain[-1][1]
                        and positions[a]-positions[chain[-1][1]-1]-1<=240)]
            if len(chains)>64:raise SourceError('quotation too ambiguous')
        chains=[c for c in chains if bounded(c)]
    if len(chains)!=1:raise SourceError('quotation missing or ambiguous; source retained')
    spans=[(start+positions[a],start+positions[b-1]+1) for a,b in chains[0]]
    return spans,'typography' if len(spans)==1 else 'ordered_excerpts'
