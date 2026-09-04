# Scanned plates

This directory holds the scanned botanical plates the web UI shows for each phase, one
`<plant>.webp` and one `<plant>-thumb.webp` per plant, plus a `SOURCES.md` that records
where each came from. They are not drawn by hand: they are cropped, downsampled copies of
public-domain chromolithographs from *Prof. Dr. Thomé's Flora von Deutschland, Österreich und
der Schweiz* (Gera, 1885), taken from Wikimedia Commons.

They are produced, not written: run

    pip install "context-garden[plates]"
    garden plants --fetch

on a machine that can reach `commons.wikimedia.org` and `upload.wikimedia.org`, then commit
the result. While a plate is missing the UI shows the drawn specimen for that plant instead.
