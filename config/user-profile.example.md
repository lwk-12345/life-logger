# User Profile Example (Optional)

Copy this file to `config/user-profile.md` (ignored by git) and customize it for your own workflow.
The activity v3 extractor reads this file (via `activity_prompt.user_profile_path`) to better separate personal life events from computer/AI dictation and background media.

## Identity & Work
- Role: Software developer working on personal projects.
- Dictation tools: Speaking to Terminal, IDE, or AI coding assistants is classified as work (`工作·项目`), not personal life.

## Timezone
- Local timezone: UTC+8 (or configure according to your preference).

## Frequent Contacts (Optional)
- Add nicknames/relationships of frequent contacts if you often take speakerphone calls, so their voice isn't confused with your own speech.

## Daily Habits
- Diet: Speak meal names, weights (grams), or read nutrition labels aloud. Configure your own weight and resting calorie expenditure in the Web UI food card.
- Fitness: Log sets, reps, and weights during workouts.
- Expenses: Say "记一笔" or mention purchases; if you correct yourself immediately, the final statement takes precedence.
- Background media: Videos, podcasts, livestreams, and TV shows played in the background should be folded into media content rather than personal life.

## Common ASR Corrections (Optional)
- `Example misheard phrase` -> `Intended word`
