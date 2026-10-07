# Recap English → Myanmar

**English လို ပြောထားတဲ့ recap / ဇာတ်လမ်းပြော video ကို မြန်မာအသံနဲ့ ပြန်ထုတ်ပေးတဲ့ Windows tool**
Narrator only · Whisper + Gemini · Microsoft Edge TTS (Myanmar) · Smart Sync

> English: turns an English-narrated recap/story video into the same video narrated in Myanmar
> (Burmese). The Myanmar voice leads, and the picture of every line is re-timed to it ("Smart Sync").
> See the [English section](#english) below.

---

## ဘာလုပ်ပေးလဲ

1. **နားထောင်** — English အသံကို စက်ထဲမှာပဲ Whisper (`small.en`) နဲ့ စာသားပြောင်းတယ်။ စကားလုံးတိုင်းရဲ့ အချိန်ကို ယူထားတယ်။
2. **အပိုင်းခွဲ** — စာကြောင်း၊ အပိုဒ်၊ အသံရပ်တဲ့နေရာတွေမှာ ခွဲပြီး အပိုင်းတိုင်းကို ၄.၂ စက္ကန့်လောက် (အများဆုံး ၅.၆ စက္ကန့်) ထားတယ်။ အပိုင်းများလေ ရုပ်နဲ့ အသံ ပိုကိုက်လေပါ။
3. **ဇာတ်လမ်း မှတ်ဉာဏ်** — Gemini က ဇာတ်လမ်းတစ်ခုလုံးကို အရင်ဖတ်ပြီး ဇာတ်ကောင်နာမည်၊ ဆက်နွယ်မှု၊ နေရာတွေကို မှတ်ထားတယ်။ ဒါကြောင့် နာမည်တွေ တစ်ပုဒ်လုံး တစ်မျိုးတည်း ဖြစ်တယ်။
4. **ဘာသာပြန်** — Gemini က တစ်ခါမှာ အပိုင်း ၄၀ စီ မြန်မာလို ပြန်တယ်။
   - ဇာတ်ကြောင်းပြောသူလို သဘာဝကျတဲ့ စကားပြောပုံစံနဲ့ ပြန်တယ်။
   - အကျဉ်းမချုပ်ဘူး၊ ချန်မထားဘူး။ အပိုင်းတစ်ခုရဲ့ အကြောင်းအရာကို ဘေးအပိုင်းထဲ မရွှေ့ဘူး။
   - ပြန်ပြီးတိုင်း စစ်ဆေးတဲ့အဆင့် (audit) က ချန်ခဲ့တာ၊ နေရာလွဲတာ၊ ထပ်နေတာတွေကို ပြန်ပြင်တယ်။
   - မြန်မာစာလုံးထဲ တခြားဘာသာ စာလုံး ညှပ်ဝင်လာရင် (ဥပမာ "ဧည့်ခန်း" နေရာမှာ "မისခန်း") Gemini ကို ပြန်မေးတယ်။
5. **အသံထုတ်** — Edge TTS မြန်မာအသံ (Thiha ယောက်ျားသံ / Nilar မိန်းမသံ) နဲ့ ထုတ်တယ်။ "Short pauses" ဖွင့်ထားရင် Edge ထည့်တတ်တဲ့ အသံတိတ်ကို ဖြတ်လို့ စာကြောင်းကြားမှာ ~၀.၂၅ စက္ကန့်ပဲ နားတယ်။
6. **Smart Sync** — မြန်မာအသံက အချိန်ကို ဦးဆောင်ပြီး ရုပ်က လိုက်ညှိတယ်။ အသေးစိတ်ကို [Smart Sync ဘယ်လိုအလုပ်လုပ်လဲ](#smart-sync-ဘယ်လိုအလုပ်လုပ်လဲ) မှာ ကြည့်ပါ။
7. **ဖြည့်စွက်** — Blur box (မူရင်း watermark/စာတန်းဖုံးဖို့)၊ Title၊ Logo၊ Extra text၊ Zoom၊ Flip တွေကို preview ပေါ်မှာ နေရာချပြီး ထည့်လို့ရတယ်။

ထွက်လာတဲ့ video မှာ **မြန်မာအသံပဲ** ပါတယ်။ မူရင်း English အသံနဲ့ နောက်ခံသီချင်း မပါဘူး။

## လိုအပ်တာ

| | |
|---|---|
| Windows | 10 / 11 (64-bit) |
| Python | **3.12, 64-bit** — [python.org](https://www.python.org/downloads/) ကနေ သွင်းပါ။ "Add python.exe to PATH" နဲ့ "tcl/tk" ကို အမှန်ခြစ်ပါ |
| FFmpeg | `ffmpeg.exe` + `ffprobe.exe` ([gyan.dev builds](https://www.gyan.dev/ffmpeg/builds/)) |
| Gemini API key | အခမဲ့ ရတယ် — [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| Internet | Gemini နဲ့ Edge TTS အတွက် |
| NVIDIA GPU | မဖြစ်မနေ မလိုဘူး။ ရှိရင် Whisper နဲ့ video render ပိုမြန်တယ် (NVENC)။ မရှိရင် CPU နဲ့ အလိုလို လုပ်တယ် |

## ထည့်သွင်းနည်း (တစ်ခါပဲ လုပ်ရ)

1. ဒီ repo ကို download လုပ်ပါ (**Code → Download ZIP** ပြီးရင် ဖြည်ပါ၊ သို့မဟုတ် `git clone`)။
2. Python 3.12 ကို သွင်းပါ (အပေါ်က ဇယားကို ကြည့်ပါ)။
3. FFmpeg ကို အောက်ပါ နေရာ ၃ ခုထဲက တစ်ခုမှာ ထားပါ:
   - PATH ထဲ
   - `C:\ffmpeg\bin\`
   - ဒီ folder ထဲမှာ `ffmpeg` ဆိုတဲ့ folder တစ်ခုလုပ်ပြီး အဲ့ထဲ (`ffmpeg\ffmpeg.exe`, `ffmpeg\ffprobe.exe`)
4. **`INSTALL_REQUIREMENTS.bat`** ကို double-click နှိပ်ပါ။ Python package တွေနဲ့ title စာရေးဖို့ လိုတဲ့ Chromium ကို သွင်းပေးပါမယ်။
5. **`.env.example`** ကို **`.env`** လို့ copy ကူးပါ။ ပြီးရင် ဖွင့်ပြီး key ထည့်ပါ:
   ```ini
   GEMINI_API_KEY=ကိုယ့်_key
   ```
   Key တစ်ခုထက် ပိုရှိရင် (Google project တစ်ခုစီက key) ဒီလို ထည့်လို့ရပါတယ်။ တစ်ခုရဲ့ ကန့်သတ်ချက် ပြည့်ရင် နောက်တစ်ခုကို အလိုလို ပြောင်းသုံးပါတယ်။
   ```ini
   GEMINI_IMAGE_PROJECT_1_KEY=...
   GEMINI_IMAGE_PROJECT_2_KEY=...
   ```

> ⚠️ `.env` ထဲမှာ ကိုယ့် key ရှိလို့ ဘယ်သူ့ကိုမှ မပို့ပါနဲ့၊ GitHub ပေါ်လည်း မတင်ပါနဲ့ (`.gitignore` ထဲမှာ ထည့်ထားပြီးသားပါ)။

## သုံးနည်း

**`RUN.bat`** ကို double-click နှိပ်ပါ။ Window မှာ အကွက် ၃ ကွက် ပါပါတယ်။

| နေရာ | လုပ်ရမယ့်အရာ |
|---|---|
| အပေါ် | **Source Video → Browse** နဲ့ English video ကို ရွေးပါ။ **Folder** နှိပ်ရင် folder ထဲက video တွေကို တစ်ခုပြီးတစ်ခု လုပ်ပါတယ် (ပြီးပြီးသား video တွေကို ကျော်ပါတယ်)။ **Gemini .env** မှာ `.env` ဖိုင်ကို ရွေးပါ (default က ဒီ folder ထဲက `.env`)။ |
| ဘယ်ဘက် | **Edge TTS Voice** (`my-MM-ThihaNeural` / `my-MM-NilarNeural`) နဲ့ **Edge TTS Speed** (−50% … +100%) ကို ရွေးပါ။ အောက်က **Run Logs** မှာ အလုပ်လုပ်နေတာတွေ ပြပါတယ်။ |
| အလယ် | **Preview** — slider နဲ့ `◀ 5s` / `5s ▶` နဲ့ ကြည့်ချင်တဲ့ အချိန်ကို ရွှေ့ပြီး **Refresh Frame** နှိပ်ပါ။ Title၊ Logo၊ Extra text ကို ဆွဲရွှေ့ပြီး နေရာချလို့ရပါတယ်။ Blur box ကို ဆွဲလို့ရပါတယ်။ |
| ညာဘက် | **Production Tools** — Zoom (1.00–1.25×)၊ Flip (ဘယ်ညာ ပြောင်းပြန်)၊ Voice vol (50–300%)၊ Short pauses၊ **+ Draw Blur Box** / Delete Selected၊ Title (စာ၊ အရွယ်)၊ Logo (ဖိုင်၊ အရွယ်၊ အလင်းဖောက်မှု)၊ Extra Text Box။ |
| အောက်ခြေ | **START / RESUME** — စတယ် (ရပ်ထားတာဆိုရင် ဆက်လုပ်တယ်)။ **STOP SAFELY** — အခု လုပ်နေတဲ့ အဆင့်ပြီးမှ ရပ်ပြီး လုပ်ပြီးသမျှ သိမ်းထားတယ်။ **Open Job Folder** — ထွက်လာတဲ့ ဖိုင်တွေ ဖွင့်ကြည့်ဖို့။ |

**ထွက်လာတဲ့ ဖိုင်:** `jobs\<video နာမည်>_<code>\edge_tts_smart_sync\final_edge_tts_smart_sync.mp4`

Job folder ထဲမှာ ကြည့်လို့ရတဲ့ ဖိုင်တွေ:
- `whisper_transcript.txt` — English စာသား
- `timestamp_translations.json` — စာပိုင်းတိုင်းရဲ့ English နဲ့ မြန်မာ
- `smart_sync_plan.json` — အချိန်ညှိထားတဲ့ plan
- `run.log` — အလုပ်လုပ်သွားတဲ့ မှတ်တမ်း

## Smart Sync ဘယ်လိုအလုပ်လုပ်လဲ

- **ရုပ်ကို အပိုင်းခွဲ** — စာပိုင်းတိုင်းက မူရင်း video ရဲ့ ရုပ်တစ်ပိုင်းကို ပိုင်တယ်။ စာပိုင်း ၂ ခုကြားက အသံတိတ်နေရာကို အလယ်ကနေ ခွဲပြီး ဘေးက စာပိုင်း ၂ ခုကို ဝေပေးတယ်။ Video ရဲ့ ပထမ frame ကနေ နောက်ဆုံး frame အထိ ဘာမှ မကျန်ပါဘူး (V2 full coverage)။
- **အရှည်ကို အသံနဲ့ ညှိ** — ရုပ်တစ်ပိုင်းစီကို မြန်မာအသံ အရှည်အတိုင်း ဖြစ်အောင် နှေးတာ / မြန်တာ (`setpts`) လုပ်တယ်။
- **အမြန်ဆုံး ၁.၂၅ ဆ** — ရုပ်ကို ၁.၂၅ ဆထက် ပိုမမြန်စေဘူး (`max_video_speed`)။ အသံက ဒီထက်တိုနေသေးရင် အဲ့အပိုင်းရဲ့ နောက်ဆုံးက ရုပ်နည်းနည်းကို ဖြတ်တယ်။ အသံက ပိုရှည်ရင် ရုပ်ကို နှေးပေးတယ်။
- **Frame ကို တွဲတွက်** — အပိုင်းတိုင်းရဲ့ frame အရေအတွက်ကို အသံရဲ့ စုစုပေါင်း အချိန်နဲ့ တွက်တယ် (30 fps)။ အပိုင်း ရာနဲ့ချီ ရှိနေတောင် ရုပ်နဲ့ အသံ frame တစ်ဝက်ထက် ပိုမကွာဘူး။ အဆုံးမှာ log က `SYNC OK: picture=… narration=… diff=…` လို့ ပြပါတယ်။
- **ပုံစံ** — 16:9 (1920×1080) ဖြည့်ပြီး Zoom / Flip ထည့်တယ်။ NVIDIA ရှိရင် NVENC၊ မရှိရင် libx264 နဲ့ ထုတ်တယ်။

## Gemini key နဲ့ ကန့်သတ်ချက်

- Gemini ကို **ဘာသာပြန်ဖို့ပဲ** သုံးတယ်။ English ကို စာသားပြောင်းတာက စက်ထဲမှာ Whisper နဲ့ လုပ်တယ်။
- အခမဲ့ key မှာ တစ်ရက်ကို သုံးလို့ရတဲ့ အကြိမ်ရေ ကန့်သတ်ထားတယ် (Google project တစ်ခုချင်းစီ အလိုက်)။ ပြည့်ရင်:
  - နောက် key ကို အလိုလို ပြောင်းတယ်။
  - Key တွေ အကုန်ပြည့်ရင် နောက် model ကို ပြောင်းတယ် (`gemini-3.5-flash → 3.5-flash-lite → 3.6-flash → 3.7-flash → 3.1-flash-lite`)။
  - အကုန်ပြည့်သွားရင် ရပ်သွားပေမဲ့ ပြီးသမျှ မပျောက်ဘူး။ နောက်နေ့ (သို့) key အသစ်ထည့်ပြီး **START / RESUME** နှိပ်ရင် ရပ်ခဲ့တဲ့နေရာကနေ ဆက်တယ်။
- Gemini ကို VPN / proxy နဲ့မှ သုံးလို့ရတဲ့ စက်ဆိုရင် `.env` ထဲမှာ `GEMINI_PROXY=http://127.0.0.1:10808` (ကိုယ့် proxy လိပ်စာ) ထည့်ပါ။ `127.0.0.1:10808` (v2rayN) ဖွင့်ထားရင် အလိုလို သုံးပါတယ်။ အမြဲ တိုက်ရိုက်ပဲ သုံးချင်ရင် `GEMINI_PROXY=none` ထည့်ပါ။ Edge TTS ကတော့ proxy မသုံးဘဲ တိုက်ရိုက် အရင်စမ်းပါတယ်။

## အဆင့်မြင့် ပြင်ဆင်ချက် (`config.json`)

Tool ဖွင့်ပြီး START နှိပ်လိုက်ရင် `config.json` ကို အလိုလို ဖန်တီးပေးပါတယ် (UI မှာ ရွေးထားတာတွေ သိမ်းဖို့ပါ)။ လိုမှပဲ ပြင်ပါ:

| Key | Default | အဓိပ္ပာယ် |
|---|---|---|
| `ffmpeg_path`, `ffprobe_path` | (အလိုလို ရှာ) | FFmpeg ဖိုင် လမ်းကြောင်း |
| `python_path` | (အလိုလို ရှာ) | `RUN.bat` သုံးမယ့် Python |
| `max_video_speed` | `1.25` | ရုပ်ကို မြန်စေနိုင်တဲ့ အများဆုံး ဆ |
| `translation_batch_max_items` | `40` | Gemini တစ်ခါမေးရင် ပါမယ့် စာပိုင်းအရေအတွက် |
| `atomic_target_duration`, `atomic_max_duration` | `4.2`, `5.6` | စာပိုင်းတစ်ပိုင်းရဲ့ အရှည် (စက္ကန့်) |
| `translation_audit` | `true` | ပြန်ပြီးတိုင်း စစ်ဆေးတဲ့ အဆင့် |
| `title_font_path` | `C:\Windows\Fonts\mmrtext.ttf` | Title / Extra text ဖောင့် |
| `overlay_blur_strength` | `18` | Blur box ရဲ့ ဝါးအား |

## ပြဿနာ ဖြေရှင်းနည်း

| တွေ့ရတာ | လုပ်ရမယ့်အရာ |
|---|---|
| `Python with the required packages was not found` | `INSTALL_REQUIREMENTS.bat` ကို ပြန် run ပါ။ Python 3.12 ကို PATH ထဲ ထည့်ထားဖို့ လိုပါတယ်။ |
| `Gemini API key မတွေ့ပါ` | `.env` ရှိမရှိ၊ `GEMINI_API_KEY=` နောက်မှာ key ပါမပါ စစ်ပါ။ UI ရဲ့ **Gemini .env** မှာ မှန်ကန်တဲ့ ဖိုင်ကို ရွေးပါ။ |
| `key pool exhausted` / 429 | ဒီနေ့ ကန့်သတ်ချက် ပြည့်သွားလို့ပါ။ Key ထပ်ထည့်ပါ (သို့) နောက်နေ့ **START / RESUME** နှိပ်ပါ။ |
| Edge TTS `NoAudioReceived` | Internet ကို စစ်ပါ။ Tool က တိုက်ရိုက် ၂ ကြိမ်၊ system proxy နဲ့ ၂ ကြိမ် ပြန်စမ်းပါတယ်။ |
| `ffmpeg` / `ffprobe` မတွေ့ | ထည့်သွင်းနည်း အဆင့် ၃ ကို ကြည့်ပါ (သို့) `config.json` မှာ `ffmpeg_path` / `ffprobe_path` ထည့်ပါ။ |
| Title မြန်မာစာ မပေါ်ဘူး | `py -3.12 -m playwright install chromium` ကို run ပါ။ ဖောင့်ကို `RECAP_MM_FONT` (environment variable) (သို့) `title_font_path` နဲ့ ပြောင်းလို့ရပါတယ်။ |
| Log မှာ `⚠️ ID 00012: removed letters of another script` | Gemini က မြန်မာစာထဲ တခြားဘာသာ စာလုံး ထည့်ခဲ့လို့ ဖယ်ထားတာပါ။ အဲ့စာကြောင်းကို `timestamp_translations.json` ထဲမှာ ကြည့်ပါ။ |

## Folder ဖွဲ့စည်းပုံ

```
RUN.bat                    UI ဖွင့်ဖို့
INSTALL_REQUIREMENTS.bat   Python package + Chromium သွင်းဖို့
.env.example               Gemini key ပုံစံ (.env လို့ copy ကူးပါ)
requirements.txt
app/
  controller.py            UI (tkinter): preview၊ overlay၊ job တွေ စီမံတာ
  planner_worker.py        အဆင့် ၁: Whisper → story context → ဘာသာပြန် → Smart Sync plan
  recap_planner.py         Planner (စာပိုင်းခွဲ၊ Gemini prompt၊ audit၊ ID စစ်တာ)
  render_worker.py         အဆင့် ၂: Edge TTS → Smart Sync render
  core.py                  Edge TTS, Short pauses, Smart Sync, overlay, mux
  preview_tools.py         Preview frame / logo
  launch.ps1               Python ရှာပြီး UI ဖွင့်တာ
recap_runtime/services/
  whisper_service.py       faster-whisper (GPU / CPU)
  ai_service.py            Gemini client (proxy option)
services/
  text_overlay_service.py  မြန်မာ title စာကို Chromium နဲ့ ပုံထုတ်တာ
tests/                     python -m unittest discover -s tests
```

## Test

```bat
py -3.12 -m unittest discover -s tests
```
Internet မလိုပါဘူး။ Gemini ကို အတုနဲ့ အစားထိုး စမ်းပါတယ်။

## သတိပြုရန်

- Edge TTS က Microsoft ရဲ့ online ဝန်ဆောင်မှုပါ။ Gemini ကတော့ Google ရဲ့ API ပါ။ သူတို့ရဲ့ စည်းကမ်းချက်တွေကို လိုက်နာပါ။
- သုံးမယ့် video ရဲ့ မူပိုင်ခွင့်ကို ကိုယ်တိုင် တာဝန်ယူပါ။

---

## English

**Recap English → Myanmar** turns an English-narrated recap/story video into a Myanmar-narrated one
on Windows.

### How it works
1. **Listen** — local Whisper (`small.en`, faster-whisper; NVIDIA GPU or CPU) writes the English
   with word timestamps.
2. **Split** — sentence/clause/pause boundaries give short sync units (about 4.2 s, at most 5.6 s);
   more units = more sync anchors.
3. **Story memory** — Gemini reads the whole transcript first and keeps names, relationships and
   places consistent.
4. **Translate** — Gemini translates 40 units per request into natural spoken Burmese narration
   (no summary, no shortening, every unit keeps its own content), then an audit pass repairs
   omissions/shifts/duplicates. Letters of another script inside the Burmese (e.g. Georgian "მის"
   inside "ဧည့်ခန်း") are asked again, and if still there removed and logged.
5. **Voice** — Microsoft Edge TTS Myanmar voices (Thiha / Nilar). "Short pauses" trims the silence
   Edge puts around each clip (~0.25 s between lines instead of ~1 s).
6. **Smart Sync** — every unit owns a window of the original picture (the silence between two units
   is split at its middle, so the whole video is used). Each window is slowed down or sped up to the
   length of its Myanmar voice; it never plays faster than 1.25× (`max_video_speed`), and when the
   voice is shorter still the end of that window is cut. Frame counts are cumulative at 30 fps, so
   even hundreds of parts stay within half a frame of the voice (`SYNC OK … diff=…` in the log).
   16:9 fill (1920×1080) with optional zoom/flip; NVENC or libx264.
7. **Production tools** — blur boxes, title, logo and an extra text box placed on the preview.

The output has only the Myanmar voice (no original audio or music).

### Requirements
Windows 10/11 64-bit · Python 3.12 64-bit (with tcl/tk, on PATH) · FFmpeg (`ffmpeg.exe` +
`ffprobe.exe` on PATH, in `C:\ffmpeg\bin`, or in a folder named `ffmpeg` here) · a free Gemini API
key ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)) · internet. An NVIDIA GPU is
optional.

### Install
1. Download the repo (Code → Download ZIP) and unzip it.
2. Run **`INSTALL_REQUIREMENTS.bat`** (pip packages + Chromium for the Myanmar title renderer).
3. Copy **`.env.example`** to **`.env`** and set `GEMINI_API_KEY=`. More keys
   (`GEMINI_IMAGE_PROJECT_1_KEY`, `GEMINI_IMAGE_PROJECT_2_KEY`, …) are used in turn when one is used
   up; after all keys the next model is tried
   (`gemini-3.5-flash → 3.5-flash-lite → 3.6-flash → 3.7-flash → 3.1-flash-lite`).
   If Gemini needs a proxy, add `GEMINI_PROXY=http://host:port` (a running v2rayN on
   `127.0.0.1:10808` is used automatically; `GEMINI_PROXY=none` forces direct).

Never share or commit your `.env` (it is in `.gitignore`).

### Use
Double-click **`RUN.bat`**:
- **Source Video** → Browse (or **Folder** for a batch; finished videos are skipped).
- **Edge TTS Voice / Speed** on the left.
- **Preview** in the middle: seek, Refresh Frame, drag the title/logo/text, draw blur boxes.
- **Production Tools** on the right: Zoom, Flip, Voice vol, Short pauses, blur, title, logo,
  extra text.
- **START / RESUME** — starts or continues; **STOP SAFELY** — stops at a safe point and keeps
  everything; **Open Job Folder**.

Result: `jobs\<name>_<code>\edge_tts_smart_sync\final_edge_tts_smart_sync.mp4`
(plus `whisper_transcript.txt`, `timestamp_translations.json`, `smart_sync_plan.json`, `run.log`).

### Advanced (`config.json`, created on the first START)
`ffmpeg_path`, `ffprobe_path`, `python_path`, `max_video_speed` (1.25),
`translation_batch_max_items` (40), `atomic_target_duration` / `atomic_max_duration` (4.2 / 5.6),
`translation_audit` (true), `title_font_path`, `overlay_blur_strength` (18).

### Tests
`py -3.12 -m unittest discover -s tests` (offline; Gemini is mocked).

### Notes
Edge TTS is a Microsoft online service and Gemini is a Google API — follow their terms. You are
responsible for the rights to the videos you process.
