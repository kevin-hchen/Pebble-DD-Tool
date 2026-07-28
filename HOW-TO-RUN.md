# MedRAG — how to run it

One page. No technical background needed. **It is free to run.**

## What it does

Type a drug and a disease. MedRAG finds the relevant clinical trials and
published research, reads them, and writes a diligence memo where every claim
has a citation you can check. It takes a few minutes and gives you a PDF.

It will not invent findings. If it cannot answer something from the sources, it
says so.

## Starting it

**Mac:** double-click **`Start MedRAG.command`**

The first time, macOS will probably refuse to open it and say something about an
*unidentified developer*. That is normal for anything downloaded rather than
installed from the App Store. To get past it:

> **Right-click** (or Control-click) `Start MedRAG.command` → **Open** → click
> **Open** again in the box that appears.

You only have to do that once. After that, a normal double-click works.

If nothing happens at all, or the file opens in a text editor instead of a black
window, double-click **`Fix permissions (Mac).command`** first, then try again.

**Windows:** double-click **`run.bat`**

Either way, the first run takes two or three minutes while it installs itself,
then a tab opens in your browser.

If it says Python is not installed, get it from
[python.org/downloads](https://www.python.org/downloads/). On Windows, tick
**"Add Python to PATH"** during installation.

*(If you are comfortable with a terminal, `bash run.sh` from this folder does the
same thing on Mac and Linux.)*

## One-time setup

The first screen asks how MedRAG should write its summaries. **Pick Groq** — it
is free, needs no credit card, and is fast enough that you will not notice.

1. Click the link on that screen to [console.groq.com/keys](https://console.groq.com/keys)
2. Sign up (email or Google), click **Create API Key**, copy it
3. Paste it into MedRAG and click **Save and continue**

That is the whole setup. The key is stored in a file on this computer and never
shown again. You will not be asked for it a second time.

The other options, if you ever need them: **Ollama** runs entirely on this
computer so nothing is sent anywhere, but needs a separate install and is slower.
**OpenAI** is the highest quality but costs about a cent or two per memo and
needs billing set up. **No AI** is always available and still produces a fully
cited memo, but it lists evidence rather than summarising it, and skips the
contradicting-evidence section — which is the most useful part.

## Making a memo

Type the drug and the disease. Click **Generate memo**. That is it.

The first memo for a new drug takes a few minutes, because MedRAG downloads the
research for it. Later memos on the same drug are much quicker — it reuses what
it already has. You do not need to load anything yourself; it handles that.

When it finishes you get a **Download memo (PDF)** button. The memo is also
saved in the `out` folder next to the app.

## Reading the memo

**Start with the Coverage box** at the top. It says how many sections actually
found evidence. If that number is low, the memo is *thin*, not reassuring —
there may be little published on this asset.

**Then read "Contradicting or unsupportive evidence"** in each section. That is
where trials that were stopped early appear, along with findings that undercut
the claim. It is the part most worth your time, and the part a founder's deck
will not have shown you.

Three phrases mean different things, and the memo keeps them apart:

- *"No contradicting evidence found"* — it looked and found nothing in what was
  loaded. Not the same as nothing existing.
- *"Not assessed"* — it did not look, because no AI provider is set up. Do not
  read this as a clean result.
- *"Not stated by sponsor"* — a trial was stopped and the sponsor filed no
  reason. That is not the same as the reason being harmless.

## When something goes wrong

**"No research could be found"** — check the spelling of the drug name, or try a
broader disease term. Some assets genuinely have very little published.

**A message about the network blocking sources** — company and campus networks
often block these. Try a different network or a phone hotspot.

**Runs fail after working before** — the free provider has a daily limit. Wait
and try again tomorrow, or switch provider under Settings.

**The start file opens in a code editor instead of running** — that happens when
the `.sh` file is opened rather than the `.command` one. Use
`Start MedRAG.command` on a Mac. If that also opens in an editor, run
`Fix permissions (Mac).command` once.

**"MedRAG cannot be opened because it is from an unidentified developer"** —
right-click the file, choose **Open**, then **Open** again. Once only.

**It asks for an email address in the black window** — you have an older copy.
Just press Enter with the field blank and it continues. Newer copies skip this.

**The folder is called "medrag 2"** — that happens when a file is downloaded
twice. Either works; just make sure you open the right one.

**Anything else** — the app shows a "Technical details" box when it fails. Send
that text to whoever maintains this. The black window also prints the full
error, so copying that is just as good.

## Getting better results at no cost

Out of the box, MedRAG uses a simple built-in method to find relevant passages.
A better one is free but needs a one-off install. In a terminal, in this folder:

```
pip install -r requirements-offline.txt
```

This downloads a small language model that runs on your own computer, improving
which passages get found. No key, no cost, nothing sent anywhere. Worth doing
once if you use this regularly.

## What it does not do

It reads abstracts, not full papers, unless PDFs have been added. It only sees
public sources — published literature and the public trial registry — never
anything private or paid. It checks that numbers in the memo appear in the cited
source, but not that they were used correctly.

Verify anything that matters before it informs a decision. It is a research aid,
not investment advice and not medical advice.
