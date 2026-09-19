"""Devanagari -> Latin, so the router sees the Hinglish it was written for.

Why this exists. Every rule in router.py is a Latin-script pattern ("kam hai",
"mere jaise", "bana de") because that is how the merchant types and how the
demo script reads. But Sarvam's `saarika` returns hi-IN transcripts in
DEVANAGARI: "sales kyun kam hai" comes back as "सेल्स क्यों कम है". Not one
rule matches, so every spoken question fell through to `unknown` and the
merchant was asked to clarify -- forever, because the clarification was spoken
too.

This is deliberately a rough phonetic transliteration, not a correct one. The
only consumer is a regex router, so "close enough to match" IS the requirement;
nothing here is ever shown to a merchant or quoted back to them. The merchant's
own words are preserved separately for attribution.

Schwa deletion is the one non-obvious rule: Devanagari consonants carry an
inherent 'a', so क + म transliterates naively to "kama", which matches no
pattern. Hindi drops that final schwa in speech, so we drop it too.
"""
from __future__ import annotations

VIRAMA = "्"

VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u",
    "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e",
}

# Single 'a' for the long matra on purpose: Hinglish writes "kya" and "raha",
# not "kyaa" and "rahaa", and the router's rules are written the way merchants
# type. Phonetic accuracy is not the goal here; matching those rules is.
MATRAS = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u",
    "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
    "ॉ": "o", "ॅ": "e",
}

CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "ळ": "l",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    # precomposed nukta forms, common in transcripts of Urdu-origin words
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f",
}

SIGNS = {
    "ं": "n",        # anusvara
    "ँ": "n",        # chandrabindu
    "ः": "h",        # visarga
    "़": "",         # standalone nukta
    "्": "",         # a virama that survived the main loop
    "‌": "", "‍": "",   # ZWNJ / ZWJ
    "।": " ", "॥": " ",
}

DIGITS = {"०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
          "५": "5", "६": "6", "७": "7", "८": "8", "९": "9"}


def has_devanagari(text: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" for ch in text)


def _syllables(word: str) -> list[list]:
    """Split into [onset, vowel, inherent] triples.

    `inherent` marks a schwa that Devanagari never wrote down -- only those may
    be deleted. An explicitly written  ा  is also an "a", but dropping it turns
    "kya" into "ky" and "raha" into "rah", which match nothing.
    """
    out: list[list[str]] = []
    i = 0
    while i < len(word):
        ch = word[i]
        nxt = word[i + 1] if i + 1 < len(word) else ""

        if ch in CONSONANTS:
            onset = CONSONANTS[ch]
            if nxt == "़":                      # combining nukta
                i += 1
                nxt = word[i + 1] if i + 1 < len(word) else ""
            if nxt == VIRAMA:                        # no vowel at all
                out.append([onset, "", False])
                i += 2
                continue
            if nxt in MATRAS:                        # explicit vowel
                out.append([onset, MATRAS[nxt], False])
                i += 2
                continue
            out.append([onset, "a", True])           # inherent vowel (schwa)
            i += 1
            continue

        if ch in VOWELS:
            out.append(["", VOWELS[ch], False])
        elif ch in MATRAS:                           # stray matra
            out.append(["", MATRAS[ch], False])
        elif ch in DIGITS:
            out.append([DIGITS[ch], "", False])
        elif ch in SIGNS:
            out.append([SIGNS[ch], "", False])
        else:
            out.append([ch, "", False])              # Latin, punctuation
        i += 1
    return out


def _word_to_latin(word: str) -> str:
    """Transliterate one word, applying Hindi's two schwa-deletion rules.

    Without these, क + म is "kama" and अ + ग + ले is "agale" -- neither of
    which any rule in the router matches. Hindi drops those schwas in speech
    and Hinglish drops them in writing, so we drop them too:

      final    कम     -> kama  -> kam
      internal अगले   -> agale -> agle   (schwa before a syllable that has
                                          its own vowel, never word-initial)
    """
    syl = _syllables(word)

    # FINAL FIRST. The internal rule below only deletes a schwa when the next
    # syllable still has a vowel, so the final deletion has to have happened
    # already -- otherwise अगर loses both and becomes "agr" instead of "agar".
    if len(syl) > 1 and syl[-1][2] and syl[-1][0]:
        syl[-1][1] = ""

    # internal, right to left; index 0 is left alone so "सबसे" does not become
    # "sbse"
    for i in range(len(syl) - 2, 0, -1):
        if syl[i][2] and syl[i][0] and syl[i + 1][1]:
            syl[i][1] = ""

    return "".join(onset + vowel for onset, vowel, _ in syl)


def to_latin(text: str) -> str:
    """Transliterate any Devanagari in `text`. Latin passes through untouched."""
    if not has_devanagari(text):
        return text
    return " ".join(_word_to_latin(w) for w in text.split())
