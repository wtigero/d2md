# OCR: what actually reads five scripts

`d2md` is built around the claim that it handles Thai correctly. That claim was only ever
tested on PDFs with a text layer. This document is the measurement that was missing, and it
starts with a failure in the shipped default.

Office formats are not involved. `.docx`, `.xlsx` and `.pptx` carry their text as text and go
to markitdown at about a second a file; `.txt` is decoded directly. Everything below is about
PDFs and images — the only inputs where the characters have to be recovered from pixels.

## The corpus

Real scanned documents are the honest test, and none of the ones on hand can be published —
they are client-confidential. So the controlled corpus is generated: ordinary insurance prose
written for the purpose, rendered through macOS's native fonts for each script, flattened to
an image and wrapped in a PDF with no text layer. `bench/make_corpus.py` builds it.

Twenty-two documents over six scripts — English, Thai, Japanese, Chinese, Korean, and Hindi.
Devanagari was later dropped from the product's scope but is kept in the corpus and in every
table here: it costs nothing to keep measuring and the numbers are the evidence for the
decision to drop it.

| Variant | What it is |
|---|---|
| `clean` | straight 200 DPI render |
| `noisy` | rotated 0.7°, blurred, speckled, contrast-reduced — a photocopy |
| `alt` | a second typeface, serif against sans |
| `mixed` | Thai and Japanese business documents with English product names and terms |

The generator refuses to run unless Pillow reports raqm support. Without HarfBuzz shaping,
Thai marks land in the wrong place and Devanagari conjuncts never form, and the corpus would
measure that rather than OCR. Both were checked by eye before any engine ran.

The `mixed` documents earned their place immediately: they are the only ones that caught a
detector bug that made real Thai invoices unreadable, and the monolingual corpus had passed
that detector cleanly. See below.

The limits that remain: one column, no tables, no handwriting, no photographs of paper at an
angle, printed at a resolution most scanners exceed. It is a floor, not a survey.

## Reading the numbers

`cer` is character error rate against the ground truth, whitespace removed. `bag` aligns each
truth line against the prediction wherever it appears in it, so neither reading order nor line
breaking costs anything — it asks only whether the characters were recovered.

Both are needed, because they disagree in an informative way. Docling reads a clean English
page perfectly but puts the heading second; `cer` charges that at 0.269 and `bag` at 0.000.
A single edit distance over the concatenated page cannot tell "misread the characters" from
"read them in a different order", and those need different fixes. Where the two diverge below,
the characters were right. `bag` in exchange cannot see duplicated or invented text, which is
why it is never quoted alone.

Both metrics arrived after runs that cost tens of seconds a page, so every prediction is kept
in the results JSON and `bench/report.py --rescore` re-grades them without running anything.
That is not tidiness — the first version of `bag` was wrong in the opposite direction, and
being able to re-score for free is what made it cheap to notice.

Nothing is ever averaged across scripts. Thai has no word boundaries and stacks diacritics,
Devanagari forms conjuncts, CJK has a glyph set in the thousands. They fail differently, and
an engine that looks good on the mean can be unusable on one of them.

## The whole comparison

`bench/report.py`, `auto` mode — no engine told which language it is looking at, which is what
a user dropping a folder on `d2md` actually gets. Cells are `clean / photocopied / alt typeface`.

| Script | shipped default | Apple Vision | RapidOCR | EasyOCR |
|---|---|---|---|---|
| English | 0.000 / 0.000 / 0.000 | **0.000 / 0.000 / 0.000** | **0.000 / 0.000 / 0.000** | 0.025 / 0.042 / 0.018 |
| Thai | 0.925 / 0.946 / 0.925 | **0.003 / 0.003 / 0.003** | 0.119 / 0.000 / 0.000 | 0.034 / 0.014 / 0.000 |
| Japanese | 0.889 / 0.830 / 0.830 | **0.000 / 0.000 / 0.000** | 0.007 / 0.000 / 0.196 | 0.026 / 0.026 / 0.046 |
| Chinese | 0.843 / 0.744 / 0.826 | 0.000 / 0.017 / 0.008 | **0.000 / 0.000 / 0.000** | 0.058 / 0.033 / 0.025 |
| Korean | 1.000 / 1.000 / 1.000 | **0.000 / 0.000 / 0.000** | 0.586 / 0.730 / 0.217 | 0.059 / 0.053 / 0.026 |
| Hindi | 1.000 / 1.000 / 1.000 | 0.853 / 0.902 / 0.853 | 0.139 / 0.391 / 0.271 | **0.068 / 0.075 / 0.030** |
| secs/page | 0.4–0.5 | 1.0–2.5 | 4.7–8.0 | 10–26 |

And the mixed-language documents, where a Thai or Japanese page carries English product names
and terms — `mixed / mixednoisy`:

| Script | shipped default | Apple Vision | RapidOCR | EasyOCR |
|---|---|---|---|---|
| Thai | 0.643 / 0.658 | **0.000 / 0.005** | 0.255 / 0.005 | 0.092 / 0.071 |
| Japanese | 0.352 / 0.379 | **0.000 / 0.007** | 0.041 / 0.034 | 0.007 / 0.007 |

The shipped column is not a slow engine reading badly. It is a fast engine being asked for the
wrong language, which is why it is the quickest column on the table and the only one that
returns nothing at all.

Apple Vision wins or ties on five of six scripts, on every typeface and on both mixed-language
pages, and it is the only engine here that needs no download. RapidOCR is the fast portable
option but unsteady in a way an average would hide — Korean swings from 0.586 to 0.217 between
typefaces, Japanese from 0.007 to 0.196 in the other direction. EasyOCR is never best except on
Devanagari and never worse than `usable` anywhere, which is what a fallback should look like.

Only the EasyOCR column is `told` the language; the rest are `auto`. Working the language out
costs EasyOCR six passes at 10–26s each, which is not a mode anyone would run.

The rest of this document is how each of those numbers was arrived at.

## What d2md shipped

`docling-auto`, the default before this work — Docling picking its own OCR engine, which on
macOS is Apple Vision with its stock Latin language list:

| Script | cer | bag | chars returned | of ~310 |
|---|---|---|---|---|
| English | 0.269 | **0.000** | 342 | all — order differs, characters correct |
| Thai | 0.969 | 0.969 | 16 | the phone number |
| Japanese | 0.928 | 0.928 | 16 | the policy number |
| Chinese | 0.917 | 0.917 | 18 | digits |
| Korean | 1.000 | 1.000 | **0** | nothing |
| Hindi | 1.000 | 1.000 | **0** | nothing |

A scanned Thai page produced `0 02116-4400` and exit code 0. Every Thai character was
discarded and nothing said so.

The cause is not the engine. It is the language list: Vision was being asked for French,
German, Spanish and English, so it returned the only characters on the page that are all four
— the digits. `docs/findings.md` §4 measured "a 6-page scanned PDF" and never recorded which
language it was; on this evidence it was English, and the result generalised to nothing.

## Apple Vision, asked properly

Same engine, told the right language:

| Script | told | auto | secs told | secs auto |
|---|---|---|---|---|
| English | 0.000 | 0.000 | 0.3 | 2.2 |
| Thai | **0.003** | 0.003 | 0.4 | 1.3 |
| Japanese | 0.000 | 0.000 | 0.4 | 1.4 |
| Chinese | 0.000 / 0.017 | same | 0.3 | 1.3 |
| Korean | 0.000 | 0.000 | 0.4 | 1.3 |
| Hindi | — | 0.861 | — | 1.0 |

Thai's 0.003 is one character in 313: `สี่งวด` read as `สิ่งวด`.

Vision has no Devanagari recognition model — the supported list is 30 languages and Hindi is
not among them, so Hindi is not a quality result but an absence. It costs nothing to install,
downloads no weights, and is the fastest thing measured.

## The language has to be chosen, and the engine will not do it

Vision's `language_preference` is an ordered preference, not a set. Asked for
`["en-US", "th-TH"]` on a Thai page it returns eleven characters at confidence 0.30; asked for
`["th-TH", "en-US"]` it returns all 313 at 1.00. Same page, same call, no error either way.

So something upstream has to pick, and confidence alone is not enough — on a degraded scan
Vision is confident about Japanese on a Chinese page. `bench/detect.py` runs the engine once
per candidate and weights each answer's confidence by how much of the returned text actually
falls in that script, with kana breaking the Japanese/Chinese tie that Han characters cannot.

That is the `auto` column above, and on monolingual pages it chose correctly every time —
`auto` equals `told` everywhere a model existed, across two engines and two typefaces.

### It failed on the first realistic document it met

Every document in the original corpus was written in one script, and that hid the failure that
real documents walk straight into. A Thai quotation quoting `Cloud Storage Premium` and
`Net 30` is 38% Latin by character count:

| Document | told | auto, before | auto, after |
|---|---|---|---|
| Thai quotation with English product names | 0.000 | **0.663** | 0.000 |
| the same, photocopied | 0.005 | **0.791** | 0.005 |
| Japanese invoice with English terms | 0.000 | **0.366** | 0.000 |
| the same, photocopied | 0.007 | **0.359** | 0.007 |

Read perfectly when told the language; unusable when left to decide. Two causes, both in the
scoring rather than the engine:

```
en: conf=0.83 chars= 73 fit=0.97 → 0.811   ← won
th: conf=1.00 chars=168 fit=0.62 → 0.619
```

Latin counted *against* Thai — the Thai reading was marked down to 0.62 for containing the
English product names it was supposed to contain, while the English reading, which returned
only those product names, scored 0.97. And the length term capped at 40 characters, so a
73-character fragment and a 168-character full reading both counted as complete.

Both are fixed in `bench/detect.py`: Latin is excluded from the denominator for non-Latin
scripts, and coverage is measured against the longest candidate on that page rather than an
absolute threshold. The second rule is the more general one — an engine asked for the wrong
language returns the fragment it could read, so *the answer that recovered the most text was
asked the right question*.

After: Thai wins at 1.00 against English's 0.35, Japanese at 1.00 against 0.64, every mixed
page returns to its `told` ceiling, and the monolingual pages are unchanged.

The lesson is about the corpus, not the code. A monolingual test set will pass a detector that
cannot survive contact with a real invoice, and the only reason this was caught is that the
corpus grew a document with two languages on the page.

Detection costs a pass per candidate: roughly 4× on Vision, 6× on RapidOCR. That is the
argument for letting the language be stated when it is known.

## RapidOCR, the portable one

ONNX, no PyTorch, one small recognition model per script fetched on demand — Thai is 7.5 MB.
Runs anywhere, which Vision does not.

| Script | cer | bag | secs |
|---|---|---|---|
| English | 0.000 | 0.000 | 0.7 |
| Thai | 0.119 / 0.000 | see below | 0.7 |
| Japanese | 0.039 | 0.039 | 1.0 |
| Chinese | 0.000 | 0.000 | 1.0 |
| Korean | 0.586 / 0.730 | unusable | 1.6 |
| Hindi | 0.139 / 0.391 | degraded | 0.7 |

Thai's 0.119 on the clean page is not a character problem — every character it returned was
correct, including `ชำระได้สี่งวด`, which Vision got wrong. Its detector missed the last line
entirely. That is a different failure from misreading, and worth knowing which one you have.

Korean is genuinely bad: the v5 model drops most lines, the v4 model returns
`7무l름 Io릉s롱&구9z0z긍8ll-0z66-lLt1`.

The version matrix is not uniform and asking for a combination that does not exist raises
rather than falling back. Probed on rapidocr 3.9.2: Thai exists only in PP-OCRv5, Japanese
only in v4, and `server`-size weights exist for Chinese alone. "Use the bigger models" is not
an option that generalises.

## EasyOCR, the one that reads everything

Torch-based, roughly 50 MB a script, and the slowest non-VLM here. Told the language,
`clean / photocopied / alt typeface`:

| Script | CER | |
|---|---|---|
| English | 0.025 / 0.042 / 0.018 | |
| Thai | 0.034 / 0.014 / 0.000 | 0.092 on the mixed-language page |
| Japanese | 0.026 / 0.026 / 0.046 | 0.007 on the mixed-language page |
| Chinese | 0.058 / 0.033 / 0.025 | |
| Korean | 0.059 / 0.053 / 0.026 | **the answer off macOS** |
| Hindi | 0.068 / 0.075 / 0.030 | **the only usable Devanagari measured** |
| secs/page | 10–26s | told the language; six times that if it has to work it out |

It is never the best engine for a script Apple Vision supports, and it is `usable` on all six
without ever being `excellent` on any. That combination is exactly what a fallback should look
like: it closes both gaps the other engines left open, and it is the only engine here that
does not have a script it cannot read at all.

The two gaps it closes were the two open questions of this whole exercise. Korean off macOS
had no answer — Vision reads it perfectly and cannot be installed, RapidOCR is 0.586 to 0.730.
EasyOCR is 0.026 to 0.059. And Devanagari had no answer at any price, including from a 2.1 GB
VLM at 17s a page.

### Devanagari was never as bad as it was scored

The first Hindi measurement said 0.192 and `degraded`. It was wrong, and the reason is worth
recording because it nearly closed a question that was open.

EasyOCR writes `4471-9920-118` on a Hindi page as `४४७१-९९२०-११८`. That is the same number in
Devanagari numerals — a defensible rendering, not a misreading — and against ASCII ground
truth every digit counted as an error. Folding digits to ASCII before scoring:

| | scored raw | digits folded |
|---|---|---|
| hi-clean | 0.192 | **0.068** |
| hi-noisy | 0.203 | **0.075** |
| hi-alt | 0.154 | **0.030** |

Folding is now part of normalisation, applied to both sides, so no engine gains or loses
against another for a choice either could make. It corrected a second engine unasked: RapidOCR
emits fullwidth digits on Japanese pages, so its Japanese was 0.007 and 0.000 rather than the
0.039 it had been scored at.

The lesson is the same one the mixed-language pages taught. A metric that has never been
looked at alongside the text it is grading will quietly answer a different question than the
one being asked — here, "does this engine spell numbers the way my ground truth does".

## A damaged text layer is not a scan, and Docling cannot tell

Routing a PDF with corrupt Thai to Docling does not fix it. Docling decides whether to OCR a
region by asking whether the page already has text there — `base_ocr_model.py` builds its OCR
rectangles from layout clusters and then "eliminate[s] clusters that intersect exclusively
with programmatic text PDF cells". The question it answers is *is there text here*, never *is
the text right*.

A broken `ToUnicode` CMap produces a text layer. It is simply the wrong characters. So Docling
reads it, finds text, declines to OCR, and passes the corruption through — the same silent
failure the tool exists to prevent, at 1s a page instead of pypdfium2's 0.005s.

The 30 files in the corpus that pypdfium2 still mangles are exactly this case. `findings.md`
§3's claim that Docling handles broken-Thai correctly was measured on a file that was broken
only for *PDFKit and pdfminer* — pypdfium2, which Docling uses, read it correctly all along.
That file was never a test of this path.

The fix is to force OCR rather than to change backend, and `force_full_page_ocr=True` is what
does it. But §3 also measured that forcing OCR on a born-digital document is actively harmful:
UUIDs corrupt (`f1c2a5e8` → `f1¢2a5e8`), `API` becomes `AP/`, table reading order is
destroyed. So it cannot be the default and it cannot be applied blindly.

What decides between them is the `ำ` test from §2. That heuristic was written to audit a
corpus, then re-purposed to make `--fast` safe; this is the third thing it turns out to be for,
and the only one where nothing else can do the job:

| Text layer | Action |
|---|---|
| absent | OCR the page — it is a scan |
| present and healthy | use it, never OCR |
| present, ≥400 Thai consonants, no `ำ` | **force full-page OCR in Thai** |

## You do not detect languages. You detect scripts.

Five more languages were added to find out how far the approach scales:
Vietnamese, German, Russian, Arabic and Traditional Chinese. Apple Vision, told the language:

| | clean / photocopied / alt typeface |
|---|---|
| Vietnamese | 0.000 / 0.004 / 0.011 |
| German | 0.000 / 0.000 / 0.000 |
| Russian | 0.000 / 0.000 / 0.000 |
| Arabic | 0.012 / 0.017 / 0.012 |
| Chinese (Traditional) | 0.000 / 0.000 / 0.000 |

All perfect or near it, Arabic included — right-to-left, and one of two cases where the corpus
broke before the engine did. GeezaPro renders Arabic correctly but has no ASCII digits, so
every policy number was tofu. Songti, chosen as the second Traditional Chinese face, is
missing nineteen characters of that page, and both engines duly "failed" it at 0.174 and
0.207 — two independent engines failing one document identically was the tell. With a font
that covers the text, Vision reads it at 0.000.

`make_corpus.py` now refuses to build a page whose font lacks a glyph for any character on it,
because a blank region scores as a misread and always makes the engine look worse than it is.

`auto` matched `told` on all of them. That looked like the detector scaling effortlessly to
eleven languages, and it is not what happened.

### The detector cannot separate languages inside one script, and does not need to

Asked which language each page is, with the scores it assigned:

```
en-clean  -> en   OK    en:1.00  zt:0.00  zh:0.00
vi-clean  -> vi   OK    vi:0.98  de:0.98  en:0.68
de-clean  -> vi   MISS  vi:1.00  de:1.00  en:0.98
zt-clean  -> zh   MISS  zt:0.75  zh:0.75  ja:0.18
```

German scored 1.00 as German and 1.00 as Vietnamese; the winner was whichever the dictionary
happened to yield first. Traditional Chinese tied with Simplified at 0.75. These are coin
flips, and no amount of tuning fixes them — the test is built on Unicode blocks and these
languages share theirs.

It costs nothing, because within a script the choice does not change the output. Reading every
Latin document under every Latin setting:

| | en-US | de-DE | vi-VT | fr-FR |
|---|---|---|---|---|
| English | 0.000 | 0.000 | 0.000 | 0.000 |
| German | 0.000 | 0.000 | 0.000 | 0.000 |
| Vietnamese | 0.000 | 0.000 | 0.000 | 0.000 |
| Vietnamese, photocopied | 0.004 | 0.004 | 0.004 | 0.004 |

Identical in every cell, Vietnamese diacritics included. Same for Han — Simplified and
Traditional settings are interchangeable on both Chinese documents.

But not across the kana boundary, which is why Japanese needs its own entry:

| | zh-Hans | zh-Hant | ja-JP |
|---|---|---|---|
| Chinese | 0.000 | 0.000 | 0.165 |
| Chinese (Trad) | 0.000 | 0.000 | 0.033 |
| Japanese | **0.627** | **0.627** | 0.000 |

### What this means for how much to claim

Seven buckets cover everything measured, and the languages inside each come free:

| Bucket | Languages Vision lists in it |
|---|---|
| Latin | en, de, fr, es, it, pt, nl, sv, da, no/nn/nb, pl, cs, ro, tr, id, ms, vi — **18** |
| Thai | th |
| Chinese | zh-Hans, zh-Hant, yue-Hans, yue-Hant |
| Japanese | ja |
| Korean | ko |
| Cyrillic | ru, uk |
| Arabic | ar, ars |

Seven OCR passes to detect, not thirty — and 24 of Vision's 30 languages claimed honestly,
because the six not listed here were not measured rather than because they fail.

The earlier framing, that every added language costs a detection pass and slows everyone down,
was wrong in a useful way. Every added *script* costs a pass. Adding French or Portuguese
costs nothing at all, because they were already covered by the pass that reads English.

### A script model is not a language model

RapidOCR covers most European languages through one `latin` recognition model, which reads
German at 0.000. It reads Vietnamese at 0.175 — and at exactly 0.175 on the clean render, the
photocopy and the alternate typeface, which is the signature of a systematic limitation rather
than noise:

```
truth   Tóm tắt hợp đồng bảo hiểm tai nạn cá nhân … được cấp … năm 2026
got     Tóm tát hop dòng bo hiém tai nan cá nhân … duçc cáp … nm 2026
```

Every base letter is right and every mark is wrong or gone. `tắt` → `tát`, `hợp` → `hop`,
`đồng` → `dòng`, `năm` → `nm` with the vowel dropped entirely. Vietnamese stacks a vowel
modifier and a tone on the same letter, and the `latin` charset does not carry the combinations.

This is the same failure as the broken Thai CMap in `findings.md` §2, arriving by a different
route: the consonants survive, the marks do not, and nothing raises. It is worth noticing that
the two languages this project cares most about — Thai and Vietnamese — are both marked scripts,
and both are where the cheap generic option quietly loses information.

So "RapidOCR supports Vietnamese" is true of its documentation and false of its output. The
support table has to be built from measurements for exactly this reason.

## What to put on the box, by platform

The tables above show every variant because the spread is informative. This asks the narrower
question — *would we claim this language* — and for that only the worst measured case counts.
A language that reads at 0.000 clean and 0.73 photocopied is not supported; it works when you
are lucky. `bench/support.py` reports it.

Worst CER over all four variants, `usable` at 0.10:

| Language | Apple Vision | EasyOCR | RapidOCR |
|---|---|---|---|
| English | **0.000** | 0.042 | **0.000** |
| Thai | **0.005** | 0.092 | 0.255 ✗ |
| Japanese | **0.007** | 0.046 | 0.196 ✗ |
| Chinese | 0.017 | 0.058 | **0.000** |
| Chinese (Trad) | **0.000** | 0.050 | 0.264 ✗ |
| Korean | **0.000** | 0.059 | 0.730 ✗ |
| Vietnamese | **0.011** | 0.026 | 0.175 ✗ |
| German | **0.000** | 0.023 | 0.113 ✗ |
| Russian | **0.000** | 0.020 | 0.116 ✗ |
| Arabic | **0.017** | 0.241 ✗ | 0.187 ✗ |
| Hindi | no model | **0.075** | 0.391 ✗ |
| **total** | **10 / 11** | **10 / 11** | **2 / 11** |

RapidOCR is much weaker under this rule than the per-variant tables suggested, and it drags
any all-engines intersection down to two languages with it. The useful intersection is the
pair — Apple Vision and EasyOCR agree on nine — and the useful statement is per platform,
picking the cheapest engine that clears the bar for each language:

**macOS — 11 of 11**

| Engine | Languages | Speed | Worst |
|---|---|---|---|
| Apple Vision | English, Thai, Japanese, Chinese, Chinese (Trad), Korean, Vietnamese, German, Russian, Arabic | 0.4s/page | 0.017 |
| EasyOCR *(extra)* | Hindi | 10.1s/page | 0.075 |

**Linux / Windows — 10 of 11**

| Engine | Languages | Speed | Worst |
|---|---|---|---|
| RapidOCR | English, Chinese | 0.7s/page | 0.000 |
| EasyOCR | Thai, Japanese, Chinese (Trad), Korean, Vietnamese, German, Russian, Hindi | 10.1s/page | 0.092 |
| — | Arabic — unsupported | | |

Two languages are single-engine and therefore platform-bound in opposite directions: Arabic
only Vision reads, Devanagari only EasyOCR reads. Everything else is available everywhere, and
the honest cross-platform claim is the nine both engines handle.

The cost difference between platforms is larger than the coverage difference. macOS reads ten
languages at 0.4s a page with nothing installed; elsewhere eight of those cost 10s a page and
50 MB a script. That is the number to put next to the language count, because a user comparing
the two will otherwise discover it themselves.

RapidOCR keeps a narrower job than it was first given: the fast, PyTorch-free option for
English and Chinese, which is what it is genuinely good at — 0.000 on both, fifteen times
faster than EasyOCR.

## PP-OCRv6: one model instead of six, and two fixes for free

RapidOCR was benchmarked with a per-language model, because that is how its API is arranged
and because Thai exists only in v5 and Japanese only in v4. PaddleOCR's documentation mentions
a v6 line whose single model "supports 50 languages", and RapidOCR exposes it — for `ch`, `en`
and `japan` only, which made it look like another partial matrix and it was skipped.

The `ch` v6 model turns out not to be a Chinese model. Run against every script, told nothing:

| Script | v6 `ch` medium | previously, per-language |
|---|---|---|
| English | **0.000** | 0.000 |
| Japanese | **0.000** | 0.007 (v4 `japan`) |
| Chinese | **0.000** | 0.000 (v4 `ch`) |
| Chinese (Trad) | **0.000** | 0.264 ✗ (v4 `chinese_cht`) |
| Vietnamese | 0.138 ✗ | 0.175 ✗ (v5 `latin`) |
| Thai | 0.895 ✗ | 0.000–0.255 (v5 `th`) |
| Korean | 0.816 ✗ | 0.730 ✗ |
| Russian | 0.850 ✗ | 0.116 ✗ (v5 `cyrillic`) |
| Arabic | 0.971 ✗ | 0.187 ✗ (v5 `arabic`) |

One model covers English, Japanese, Chinese and Traditional Chinese at 0.000, and in doing so
repairs the two worst RapidOCR results in that group: Traditional Chinese from 0.264, Japanese
from 0.007. It is a CJK-and-Latin model rather than the 50-language model the phrasing
suggests — Thai, Korean, Cyrillic and Arabic are not in it — but for the four it does cover it
replaces four separate downloads and beats all of them.

The lesson is the same one this document keeps repeating: the vendor's language list said
"Chinese" and the measurement said four languages, in the same direction that the Vietnamese
`latin` claim went the other way. Neither is usable without running it.

## Tesseract

Installed and correct on English. On Thai it returns the right characters spaced one glyph
apart — `ส ร ุ ป ก ร ม ธร ร ม ์` — and decomposes `จำนวน` into `จํานวน`, the same broken-`ำ`
shape `findings.md` §2 documents in PDF text layers. It also corrupted digits in every run
(`4471-9920-118` → `4471-99209-118`).

Docling's Tesseract CLI wrapper additionally raises `KeyError: 'text'` on this machine, so it
cannot be used through Docling at all without a patch. Not pursued further.

## Local VLMs

`bench/vlm.py`, through Docling's MLX pipeline on an M3.

**granite-docling 258M** — the smallest thing that works, ~3s/page:

| Script | bag | |
|---|---|---|
| English | 0.000 | |
| Thai | 0.173 / 0.854 | falls apart on the photocopy |
| Japanese | 0.000 / 0.013 | |
| Chinese | 0.008 / 0.033 | |
| Korean | 0.020 / 0.072 | |
| Hindi | 0.301 | unusable |

Excellent on CJK for 258 MB, and roughly ten times slower than Vision. It does not solve
either of the two scripts that needed solving.

**GLM-OCR bf16** — 2.1 GB of weights, 12–47s per page:

| Script | cer | |
|---|---|---|
| English | 0.134 | |
| Thai | 0.718 / 0.667 | unusable |
| Japanese | 0.007 / 0.000 | |
| Chinese | 0.000 / 0.000 | |
| Korean | 0.092 / 0.053 | |
| Hindi | 0.256 / **0.135** | the best Devanagari result measured |

Two hundred times slower than Vision per page, worse at Thai than a 7.5 MB ONNX model, and
still only *degraded* on the one script it leads. These are order-sensitive figures — this run
predates `cer_bag` and its predictions were not kept, so English's 0.134 may be partly markdown
formatting rather than misread characters. It does not change the conclusion.

The general lesson is that a document VLM's OmniDocBench standing says little about the
scripts it was not trained on. Both models here are excellent at CJK, mediocre at Thai and
poor at Devanagari, while a 7.5 MB recognition model reads Thai correctly.

There is a cost to having them installed at all. Adding `mlx-vlm` pulled transformers from
5.8.1 to 5.15.0, and on that version Docling's layout model stops compiling on MPS —
`torch._inductor.exc.InductorError: KeyError: torch.float64`, every PDF failing. Pinning
transformers back to 5.8.1 restores it. A VLM tier that breaks the default tier by being
installed is not a tier; whatever ships must keep them apart.

## Typhoon OCR — evaluated, not adopted

Raised as a Thai-specific option. Its model card states Thai and English only, on a Qwen3-VL
2B base, and recommends against the GGUF builds on accuracy grounds. Two of the six scripts,
at 2B parameters and seconds per page, against Apple Vision's 0.003 on Thai at 0.4s with
nothing to install. There is no room above 0.003 that justifies the cost, so it was not pulled.

## Running both engines does not work

Vision and RapidOCR fail on different scripts, which suggests running both and keeping the
better answer. Measured, it does not pay:

| Script | Vision alone | RapidOCR alone | both, best-scoring wins |
|---|---|---|---|
| English | 0.000 | 0.000 | 0.000 |
| Thai | 0.003 | 0.119 | 0.003 |
| Japanese | **0.000** | 0.039 | **0.039** |
| Chinese | 0.000 | 0.000 | 0.000 |
| Korean | 0.000 | 0.586 | 0.000 |
| Hindi | no model | 0.139 | 0.139 |

Japanese gets worse, and it still does after the detection fix below — RapidOCR scores 0.984
against Vision's 0.917 on the same page while reading it worse. The selector prefers
RapidOCR's answer on a page Vision read perfectly,
because confidence-weighted-by-script-fit cannot tell two plausible readings of the same
script apart — it was built to catch the wrong *script*, and asking it to judge quality within
a script is a different question it was never given evidence for. Auto mode also costs several
seconds a page and up, since every candidate is tried on both engines.

It is not reliably wrong either, which is worse than being reliably wrong: on the serif
Japanese face the ordering reverses and Vision wins, 0.917 against 0.831. A selector that
picks the better engine on one typeface and the worse one on another cannot be reasoned
about.

The useful part of the idea survives without the ensemble: route by *capability*, not by
score. Vision has no Devanagari model, so Hindi goes to RapidOCR; everything else Vision
supports goes to Vision. That is a lookup table, it costs nothing, and it never picks a worse
answer for a script where one engine is known to be better.

## Where this leaves the three levels

| | What it does | Thai | Speed | Downloads |
|---|---|---|---|---|
| `fast` | pypdfium2 text layer, no OCR | correct where a text layer exists | 0.31s / 56 pages | none |
| `balanced` | Docling layout + tables, OCR routed by script | 0.003 | 0.4–1.6s / page | 0–8 MB |
| `best` | only meaningful off macOS with Korean — EasyOCR | 0.003 | 10–26s / page | 50 MB / script |

The recommendation is that the OCR engine is **not** the quality dial. Character accuracy is
already at its ceiling in the middle tier — 0.003 on Thai, 0.000 on four other scripts — and
nothing measured here improves on it at any price. What the top tier buys is structure on
documents the layout model reads badly, and it should be sold as that rather than as accuracy.

Engine choice, by capability rather than by score. Three engines cover all six scripts, and
each is there because it is the best answer to a question the others cannot answer:

| Script | On macOS | Anywhere | Size |
|---|---|---|---|
| English | **Vision** 0.000 | RapidOCR 0.000 | 0 / 8 MB |
| Thai | **Vision** 0.003 | RapidOCR 0.000–0.119 | 0 / 8 MB |
| Japanese | **Vision** 0.000 | RapidOCR 0.007 | 0 / 8 MB |
| Chinese | **Vision** 0.000–0.017 | RapidOCR 0.000 | 0 / 8 MB |
| Korean | **Vision** 0.000 | **EasyOCR** 0.026–0.059 | 0 / 50 MB |

Read as a rule: Apple Vision wherever it has a model, RapidOCR as the fast portable default.
On macOS that is the whole story and it needs nothing installed at all.

**Devanagari is out of scope.** It was measured and it works — EasyOCR reads it at 0.030 to
0.075, better than a 2.1 GB VLM — and the numbers are kept below because they cost something
to obtain and may be wanted later. It is excluded because no document in the reference corpus
uses it, not because nothing can read it.

That leaves exactly one cell needing an engine the other two cannot supply: **Korean off
macOS**. RapidOCR's Korean cannot be rescued by configuration — v4 scores 0.586, v5 0.546, and
raising the render resolution makes it worse rather than better, because the text detector is
missing whole lines rather than misreading them. So a portable build that must read Korean
carries EasyOCR and its 10–26s a page; one that need not is RapidOCR alone, at 8 MB a script
and no PyTorch.
