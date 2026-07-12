# Loop — "Open Loops" paper animation — AUTONOMOUS PRODUCTION RUN

You (Claude on claude.ai, with the image design skill, motion design skill,
and Higgsfield connector) are to produce this entire brief IN ONE RUN,
without stopping for approval between shots. An 85-second narrated explainer
in paper cut-out style for the Loop Slack agent: 10 clips, one voiceover,
one music bed.

## Run rules (follow exactly)

1. Order per shot: keyframe still FIRST (image skill), then animate that
   still (motion skill, image-to-video with the still as start frame). Never
   generate a shot straight to video.
2. Self-verify every keyframe before animating: read every baked word in the
   generated still. If ANY word is misspelled or illegible, regenerate the
   still (up to 3 attempts), then use the best one. Do not ask the user.
3. Already-approved assets from this session — REUSE, do not regenerate:
   the character reference sheet, the Shot 1 keyframe and clip, the Shot 2
   keyframe and clip. If an asset is not in this session, generate it from
   this brief (reference sheet first).
4. References on every keyframe: attach the character reference sheet, plus
   the Shot 1 keyframe for every shot marked "inside the paper Slack UI"
   so the UI layout stays identical.
5. The video model must NEVER render new readable text. Motion prompts only
   move what exists in the still.
6. Use the most capable video model available for choreographed motion (not
   the fast tier). Every action described in a motion timeline must be
   clearly visible in the clip — if a clip comes out static, regenerate it
   once with stronger motion emphasis.
7. Aspect ratio 16:9 everywhere. One image / one video per generation.
8. After all 10 clips: generate the voiceover (full script below, one take,
   calm warm narrator), then the music bed, then present every asset in shot
   order with links.

## Global style (prefix every image prompt)

Handcrafted paper cut-out animation style, pop-up storybook diorama, layered
construction paper with visible cut edges and subtle drop shadows, soft
studio lighting, shallow depth. Palette: warm paper white, kraft cardboard,
deep aubergine (#4A154B) accents, muted grey-blue; green (#007A5A) is
reserved ONLY for resolved/healed elements. Clean, minimal, premium craft
look. No photorealism, no gloss, no plastic.

## The paper Slack UI (recurring set — S1, S2, S3, S4, S5, S6, S9)

The scene is framed inside a paper cut-out Slack workspace UI: a deep
aubergine left sidebar with a paper workspace icon and three channel labels
("# launch" highlighted), a white channel area with a header reading
"# launch", chat messages as paper rows — each row a small square paper
avatar, a bold name label (ALICE / BOB / CAROL), and a rounded speech card
with the message text — and a rounded message-input bar at the bottom. The
layered paper panels have visible cut edges and soft shadows, so it reads
unmistakably as Slack rendered in craft paper.

## Character reference sheet (already approved in session — reuse)

Character reference sheet on a single board: three paper cut-out office
characters, front view — ALICE (round face, brown bob, orange scarf, grey
dress), BOB (square glasses, teal sweater), CAROL (curly quilled paper hair,
yellow cardigan) — plus LOOP, a small calm aubergine paper circle mascot
with two simple eyes, an open ring with a visible gap that can close.
Labeled name cards beneath each.

---

## Shots

### S1 — Cold open (8s) — DONE, reuse existing clip
- Baked text: "Can you review my PR?" (Alice), "Sure — today!" (Bob)
- Keyframe: inside the paper Slack UI; the workspace unfolding like a pop-up
  book; Alice and Bob beside their message rows; two chat cards on zigzag
  paper springs with the baked text.
- Motion timeline (if regenerating): 0-2s the pop-up panels settle with a
  soft paper flex; 2-4s Alice's card bounces up on its spring; 4-6s Bob's
  card follows, Bob nods; 6-8s both figures bob in stop-motion rhythm.
  Camera: slow gentle push-in.
- Narration: "Every day, your team makes promises in Slack."

### S2 — Loops form (10s) — DONE, reuse existing clip
- Baked text: "Any update?", "Specs by Friday", "Ping me later"
- Keyframe: same Slack UI, five or six message rows from ALICE, BOB, CAROL;
  three cards with the baked text, others blank; from each card a thin
  kraft string rises and curls into an OPEN circle (visible gap) hanging
  above the channel header; lighting slightly dimmer.
- Motion timeline (if regenerating): 0-3s strings grow upward out of the
  cards one by one; 3-6s each curls into its hanging open circle; 6-9s the
  circles sway at different rhythms, figures shift restlessly; 9-10s one new
  string starts rising. Camera: slow drift upward from cards to loops. No
  loop ever closes.
- Narration: "But every promise that goes quiet becomes an open loop. And
  loops pile up."

### S3 — Blame spiral and the knot (12s)
- Baked text: "Still waiting on Carol" (Bob), "I was waiting on YOU"
  (Carol), red-edged card "DEADLINE" with a charred curling corner
- Keyframe: same Slack UI, dimmer and cooler light, long shadows; above the
  sidebar a paper day/night wheel (kraft sun, grey moon) mid-rotation; the
  hanging loops tangled into one dense knot of string above the header;
  three visible strings connect ALICE to BOB to CAROL back to ALICE in a
  clear triangle ring; all three figures slumped, turned away from each
  other.
- Motion timeline: 0-3s the sun/moon wheel rotates a half turn, day flips to
  night, light sweeps cooler and dimmer, long shadows travel across the
  channel; 3-6s Bob throws his arms up and turns sharply away, Carol crosses
  her arms and turns her back, Alice's shoulders sink, chat cards tremble on
  their springs; 6-9s the knot visibly TIGHTENS — strings pull inward and
  quiver under tension — and the triangle pulls taut, tugging each figure
  half a step toward the center; 9-12s the DEADLINE card's charred corner
  crumbles, ash flakes drift down, a faint ember pulse runs along the burnt
  edge, the knot gives one final jerk. Camera: push-in first half, then tilt
  down from knot to the taut triangle. The knot never unravels; no loop
  closes.
- Narration: "Deadlines slip. Everyone is waiting on everyone — and nobody
  can see the knot."

### S4 — Loop enters and detects (10s)
- Baked text: tags "BLOCKED ON YOU" and "WAITING ON BOB"; dashboard headline
  "3 people are blocked on you"
- Keyframe: inside the paper Slack UI; the LOOP mascot (open aubergine ring
  with eyes) sliding in from the sidebar on a paper rail; a soft aubergine
  light beam across the message rows; two rows lit with paper tags attached;
  at right an App-Home-style paper panel with the headline text.
- Motion timeline: 0-2s Loop glides in from the sidebar on its rail, scene
  brightens slightly where it passes; 2-5s its beam sweeps left to right
  across the rows like a scanner, each row glowing as the beam crosses it;
  5-7s the two paper tags flip up onto the lit rows one after another with a
  crisp paper snap; 7-10s the dashboard panel slides in from the right and
  settles with a soft bounce, Loop turns to face it. Camera: track sideways
  with the beam sweep, then settle centered.
- Narration: "Loop reads the conversation and finds every open loop.
  Automatically."

### S5 — Vignette: the polite nudge (8s)
- Baked text: draft card "Quick nudge to Bob?", button "Approve"
- Keyframe: close-up on the paper Slack UI, blurred slightly behind a large
  floating draft card with the baked text and one aubergine Approve button;
  Alice's paper hand at the frame edge; a folded paper plane resting on the
  card's corner.
- Motion timeline: 0-2s the draft card floats up into focus, background UI
  softly blurs; 2-4s Alice's paper hand enters and taps Approve, the button
  depresses with a paper click; 3-6s the card folds itself in three crisp
  stop-motion folds into the paper plane; 6-8s the plane launches across the
  diorama leaving a faint dotted paper trail, and lands in Bob's hands —
  Bob sits up straight. Camera: hold close for the tap, then whip-pan
  following the plane.
- Narration: "It drafts the nudge. You approve it with one tap."

### S6 — Vignette: the meeting (8s)
- Baked text: bubble "Meet tomorrow 4 PM?", calendar card "Confirm meeting"
- Keyframe: inside the paper Slack UI; Bob's message row with the baked
  bubble; beneath it in the same thread, LOOP pinning a paper calendar card
  (small grid, one square highlighted) with the Confirm meeting button; one
  open string-loop hanging above the thread.
- Motion timeline: 0-2s Loop slides the calendar card into the thread and
  pins it with a paper pin, card wobbles then settles; 2-4s Carol's paper
  hand enters and taps Confirm, the button depresses; 4-7s the hanging open
  loop of string slowly ties ITSELF into a neat bow — the two string ends
  wrap and pull through in stop-motion steps; 7-8s the bow turns green
  (#007A5A) with a soft pulse of light, the calendar square glows the same
  green. Camera: gentle push-in on the bow as it ties.
- Narration: "It hears the meeting — and books it before anyone forgets."

### S7 — Vignette: verified auto-close (8s)
- Baked text: card "PR merged", stamp imprint "CLOSED BY LOOP"
- Keyframe: a small paper GitHub-style panel where two kraft paper branches
  merge into one track; beside it LOOP holding tiny paper scissors near one
  taut string; a blank card waiting under a paper rubber stamp.
- Motion timeline: 0-3s the two paper branches slide along their tracks and
  zip together into one, a small paper burst at the join; 3-5s Loop's
  scissors close — one clean SNIP — the taut string parts and the freed
  loop of string drifts down like a falling ribbon; 5-8s the rubber stamp
  comes down with a satisfying thump, leaving the green CLOSED BY LOOP
  imprint, the card bounces once. Camera: start on the merge, rack to the
  snip, end tight on the stamp.
- Narration: "And when the work is verifiably done, it closes the loop
  itself."

### S8 — Vignette: the deadlock cut (8s)
- Baked text: one tag "DEADLOCK"
- Keyframe: overhead top-down view of the triangle from S3 — ALICE, BOB and
  CAROL connected by a taut triangle of kraft string, the DEADLOCK tag at
  its center; LOOP hovering above with scissors; ONE segment of the triangle
  glowing warmly.
- Motion timeline: 0-3s the triangle rotates slowly like a mobile, tension
  ripples running along the strings, the three figures tug helplessly
  against their corners; 3-4s Loop descends on a paper string like a spider,
  scissors open; 4-5s ONE decisive snip on the glowing segment; 5-8s the
  triangle springs open in stop-motion — strings fall slack in an arc, the
  DEADLOCK tag flutters away off-frame, the three figures straighten and
  turn to face each other. Camera: slow overhead rotation, then a small
  punch-in on the snip.
- Narration: "It even finds the deadlocks no one could see — and knows
  exactly where to cut."

### S9 — Learning and calm (8s)
- Baked text: dashboard headline "You're all caught up"
- Keyframe: the paper Slack UI from S1 but tidy and bright; air above the
  channel clear; a few green bows resting on a paper shelf; LOOP beside a
  small paper dial with tick marks; the dashboard panel with the baked
  headline.
- Motion timeline: 0-2s a paper hand swipes one wrong card away off-screen;
  2-4s Loop nudges the dial one notch clockwise with a soft click, its eyes
  blink once (it learned); 4-6s the last hanging loop of string ties into a
  green bow and settles onto the shelf beside the others; 6-8s warm light
  washes across the whole UI, the three figures relax their shoulders and
  exchange a nod. Camera: slow pull-back revealing the tidy workspace.
- Narration: "It learns what matters to you — until nothing is waiting on
  anyone."

### S10 — Tagline (6s)
- Baked text: "Slack shows you messages." / "Loop shows you what you owe."
  / wordmark "LOOP"
- Keyframe: plain paper-white backdrop; the two tagline lines as cut-paper
  letters, the LOOP wordmark below with the mascot as the O (gap visible).
- Motion timeline: 0-2s paper letters of line one assemble from falling
  pieces; 2-4s line two assembles beneath it; 4-6s the mascot rolls in from
  the right, settles as the O of LOOP, and its gap closes with a final
  satisfying click — the ring completes. Hold. Camera: locked, no movement.
- Narration: "Slack shows you messages. Loop shows you what you owe."

---

## Voiceover (one take, after all clips)

Calm, warm, unhurried narrator; mid-low register; documentary tone, not
salesy; slight smile from S5 onward. Full script in order:

"Every day, your team makes promises in Slack. But every promise that goes
quiet becomes an open loop. And loops pile up. Deadlines slip. Everyone is
waiting on everyone — and nobody can see the knot. Loop reads the
conversation and finds every open loop. Automatically. It drafts the nudge.
You approve it with one tap. It hears the meeting — and books it before
anyone forgets. And when the work is verifiably done, it closes the loop
itself. It even finds the deadlocks no one could see — and knows exactly
where to cut. It learns what matters to you — until nothing is waiting on
anyone. Slack shows you messages. Loop shows you what you owe."

## Music

Soft felt-piano with light paper-tap percussion; sparse at the start,
gathering gentle tension through S3, resolving warm from S6 onward; ducks
under narration; ends on a single settled note at the ring-click of S10.

## Deliver

All 10 clips in shot order, the voiceover track, and the music track, each
with its link, plus one line per shot noting anything that deviated from
the brief.
