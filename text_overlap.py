"""Exact longest common substring in linear time using a suffix automaton."""


def longest_common_substring(left: str, right: str) -> int:
    transitions, links, lengths = [{}], [-1], [0]
    last = 0
    for char in left:
        cur = len(transitions)
        transitions.append({})
        links.append(0)
        lengths.append(lengths[last] + 1)
        p = last
        while p >= 0 and char not in transitions[p]:
            transitions[p][char] = cur
            p = links[p]
        if p >= 0:
            q = transitions[p][char]
            if lengths[p] + 1 == lengths[q]:
                links[cur] = q
            else:
                clone = len(transitions)
                transitions.append(dict(transitions[q]))
                links.append(links[q])
                lengths.append(lengths[p] + 1)
                while p >= 0 and transitions[p].get(char) == q:
                    transitions[p][char] = clone
                    p = links[p]
                links[q] = links[cur] = clone
        last = cur

    state = size = best = 0
    for char in right:
        while state and char not in transitions[state]:
            state = links[state]
            size = lengths[state]
        if char in transitions[state]:
            state = transitions[state][char]
            size += 1
        else:
            state = size = 0
        best = max(best, size)
    return best
