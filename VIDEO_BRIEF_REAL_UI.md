# Loop — real Slack UI explainer — AUTONOMOUS PRODUCTION RUN

You (Claude on claude.ai, with the image design skill, motion design skill,
and Higgsfield connector) produce this entire brief in one run without
stopping for approval. A ~70-second product explainer showing the Loop agent
inside a realistic Slack interface, screen-demo style: 9 clips + voiceover +
music. Every feature of the product appears; the visual treatment always
pulls the eye to Loop.

## Run rules

1. Per shot: keyframe still FIRST (image skill), then animate it (motion
   skill, image-to-video, still as start frame). Never straight to video.
2. If the user provides a real screenshot for a shot, use it as the start
   frame directly — do not generate a replacement. Generate frames only for
   shots without a screenshot.
3. Self-verify every generated keyframe: read every word of UI text; if any
   word is wrong, regenerate (max 3 tries), pick the best. Do not ask.
4. The video model never renders new readable text; motion prompts only move
   and highlight what exists in the still.
5. Use the most capable video model for choreographed motion. If a clip
   comes back static, regenerate once with stronger motion emphasis.
6. 16:9 everywhere, one image / one video per generation.
7. After all clips: voiceover (script below), then music, then deliver all
   assets in shot order with links.

## Global style (prefix every generated keyframe)

A pixel-clean, realistic Slack desktop app interface, dark aubergine
(#4A154B) left sidebar with workspace name "LAUNCH CO" and channel list
("# launch" active), white message pane, crisp readable sans-serif UI text,
authentic Slack layout: avatars, bold usernames, timestamps, message input
bar. Flat, modern, high-fidelity product UI — like a real screen recording
frame, not an illustration.

THE LOOP TREATMENT (apply in every shot): everything belonging to Loop — its
bot messages, buttons, tags, panels, the Loop app icon (an aubergine open
ring) — carries a subtle aubergine glow and full saturation, while the rest
of the UI sits a touch dimmer and desaturated. Loop is always the brightest
thing on screen.

## Cast (consistent across shots)

Users: Alice (orange-accent avatar), Bob (teal), Carol (yellow). The tracked
user viewing the screen is Alice. The app: "Loop" with the open-ring icon,
bot badge "APP".

---

## Shots

### U1 — Detection in the channel (8s)
- UI text (exact): channel header "# launch"; ALICE: "Can you review my PR?"
  BOB: "Sure — today!" CAROL: "Let's meet tomorrow at 4 PM"; a thin Loop
  underline-tag beneath Bob's and Carol's rows reading "open loop detected ·
  0.92" and "meeting detected · 4:00 PM".
- Keyframe: the real #launch channel with those three messages; the two Loop
  tags just materializing beneath their rows, glowing softly.
- Motion timeline: 0-2s messages sit still, a reader's eye settles; 2-4s a
  faint aubergine scan-line sweeps down the message pane once; 4-6s the tag
  under Bob's message fades in and pulses once; 6-8s the tag under Carol's
  meeting message fades in and pulses. Cursor never moves; Loop works alone.
- Narration: "Loop watches your channels and recognizes every promise the
  moment it's made."

### U2 — The App Home reveal (10s)
- UI text (exact): header "3 people are blocked on you"; context line "Loop
  found these automatically · 12 loops tracked"; button "Review blocked
  loops"; section "Blocked on you" with descriptor "People who can't move
  until you act"; three rows — "Review the onboarding design doc" (Alice
  waiting, "2d"), "Merge the widgets PR" ("5h"), "Sign off the Q3 launch
  checklist" ("*4d overdue*" in bold); each row has a primary "Reply" button
  and a "⋮" overflow.
- Keyframe: the Loop App Home tab inside Slack, exactly that layout, Loop
  ring icon at top.
- Motion timeline: 0-2s the sidebar's Loop app entry glows, cursor clicks
  it; 2-4s the App Home slides in, hero header lands first with a soft
  settle; 4-7s the three rows cascade in top to bottom, each aging chip
  popping in after its row, the bold "4d overdue" pulsing once; 7-10s cursor
  hovers the Review button, it brightens. Camera: static screen, elements
  animate.
- Narration: "One dashboard shows exactly who's waiting on you — and how
  long they've waited."

### U3 — Spotlight and chain impact (7s)
- UI text (exact): eyebrow "Start here — highest impact"; line "Carol has
  been waiting 4d — overdue on: Sign off the Q3 launch checklist"; meta
  "clearing it unblocks 3 people"; button "Draft a reply".
- Keyframe: the spotlight card at the top of App Home; beneath it, faint,
  three small avatars connected by a chain of lines to Carol's item.
- Motion timeline: 0-2s the spotlight card lifts slightly off the page with
  a glow; 2-5s the chain of three avatars lights up link by link away from
  Carol's item, showing the downstream people; 5-7s "clearing it unblocks 3
  people" underlines itself, cursor moves to Draft a reply. 
- Narration: "It even knows which reply unblocks the most people downstream."

### U4 — The nudge composer, send as you (8s)
- UI text (exact): modal title "Send a reply"; lead "Carol is waiting on
  you: Sign off the Q3 launch checklist"; an editable text box containing
  "On it — sign-off coming this afternoon."; hint "Loop drafted this for you
  — edit it however you like."; footer "Loop will send this as you · in
  #launch"; submit button "Send as you".
- Keyframe: that modal over a dimmed Slack window.
- Motion timeline: 0-2s modal springs open from the clicked button; 2-4s a
  text cursor blinks in the drafted message, two words retype themselves
  (the user editing); 4-6s cursor clicks "Send as you", button depresses;
  6-8s the modal closes and the message appears in #launch under ALICE's own
  name, with a small Loop ring badge beside the timestamp glowing once.
- Narration: "Loop drafts the message in your voice. You edit, approve, and
  it sends as you. Nothing goes out without your tap."

### U5 — The meeting flow (9s)
- UI text (exact): Carol's message "Let's meet tomorrow at 4 PM"; in the
  App Home a row "Sync on the launch plan" with chip "proposed for Jul 12 at
  4:00 PM" and button "Schedule it"; in the thread a Loop bot message
  "@Bob — proposing Sync on the launch plan on Jul 12 at 4:00 PM. One tap to
  lock it in." with buttons "Confirm meeting" and "Add to Google Calendar".
- Keyframe: split composition — App Home row with the chip and Schedule it
  button on the left, the in-thread Loop proposal message on the right.
- Motion timeline: 0-2s the "proposed for" chip pulses under the row; 2-4s
  cursor clicks "Schedule it", a small Google Calendar event card flashes in
  a corner (prefilled title and time) then tucks away; 4-6s the Loop
  proposal message posts into the thread with a slide-up; 6-8s a hand cursor
  (Bob's side) clicks "Confirm meeting"; 8-9s the whole row sweeps green and
  flies down toward the Closed section.
- Narration: "Say a time in chat and Loop hears it — calendar event ready,
  one tap for your teammate to confirm, loop closed."

### U6 — Verified auto-close via GitHub (7s)
- UI text (exact): section header "Closed by Loop", descriptor "Handled
  automatically while you were away"; entry "Resolved with Bob — PR merged"
  with subline "Jul 11 · closed automatically · verified via GitHub"; beside
  it a small GitHub merge icon (purple merged pull request symbol).
- Keyframe: the Closed by Loop feed with that entry newest at top, the
  GitHub merge icon linked to it by a thin line.
- Motion timeline: 0-2s the GitHub merge icon flips from open to merged
  (arrow joins the branch); 2-4s a pulse travels along the thin line from
  the icon into the Loop feed; 3-5s the new entry slides in at the top of
  the feed, pushing older entries down; 5-7s "verified via GitHub" shimmers
  once. 
- Narration: "When the pull request actually merges, Loop verifies it on
  GitHub and closes the loop itself — no one files the update."

### U7 — Deadlock detection (8s)
- UI text (exact): section header "Deadlocks — circular blocks"; line
  "Everyone in this ring is waiting on someone else."; ring "Alice → Bob →
  Carol → Alice"; line "Start with Bob — Send the API estimate"; button
  "Draft the first move".
- Keyframe: the deadlock card in App Home; above the text a small circular
  diagram of the three avatars with arrows forming a closed ring, one arrow
  segment highlighted.
- Motion timeline: 0-3s the three arrows of the ring animate flowing in a
  circle, endlessly chasing; 3-5s the ring slows and the highlighted segment
  (Bob's) brightens while the others dim; 5-8s cursor clicks "Draft the
  first move", the highlighted arrow snaps and the ring uncoils into a
  straight line. 
- Narration: "Three people waiting in a circle — invisible to each of them.
  Loop sees the ring, and knows exactly which message breaks it."

### U8 — The learn loop (7s)
- UI text (exact): one App Home row "Lunch order for Friday?" with the
  overflow menu open showing "Snooze / Delegate / Dismiss"; footer line
  "Blocked on you: 2 · Waiting on others: 4 · Auto-closed: 6" and second
  line "Surfacing gate 0.50 — tuned continuously by your Confirm / Dismiss
  feedback".
- Keyframe: that row with the open overflow menu, footer visible below.
- Motion timeline: 0-2s cursor clicks "Dismiss" in the overflow; 2-4s the
  row folds up and vanishes, remaining rows slide together; 4-6s in the
  footer, "0.50" ticks up to "0.55" with a tiny odometer roll and glows;
  6-7s hold on the footer. 
- Narration: "Dismiss what doesn't matter, and Loop recalibrates — it
  learns your bar."

### U9 — Assistant and tagline (8s)
- UI text (exact): the Slack assistant pane with the user typing "Who's
  blocked on me?" and Loop replying with two compact cards ("Carol · 4d ·
  Q3 checklist", "Bob · 5h · widgets PR"); then a closing title card:
  "Slack shows you messages." / "Loop shows you what you owe." / wordmark
  "LOOP" with the open-ring icon as the O.
- Keyframe: split — assistant pane conversation on the left, and reserve the
  right third for the title card area (initially empty).
- Motion timeline: 0-2s the typed question sends; 2-4s the two answer cards
  deal in like playing cards; 4-6s the Slack UI slides left and fades as the
  title card lines fade in on clean white; 6-8s the ring icon rolls in as
  the O of LOOP and its gap clicks closed. Hold.
- Narration: "Ask it anything about your open loops. Slack shows you
  messages. Loop shows you what you owe."

---

## Voiceover (one take, after all clips)

Confident, warm product-narrator, mid register, measured pace. Script in
order: the nine narration lines above, verbatim.

## Music

Minimal electronic pulse with soft key pads; a light tick percussion that
suggests a clock through U1-U3, opening warmer from U5, single resolved
chord at the final ring click.

## Deliver

All 9 clips in shot order, voiceover, music, links for each, plus one line
per shot on any deviation from the brief.
