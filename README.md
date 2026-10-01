# CHiP Bot

CHiP is CMON's Discord translation, member-chat, contest, and role bot. This
build replaces the unofficial Google translation endpoint that occasionally
posted an HTML `Error 500` page into Discord.

## Translation setup

- Spanish source messages use `gpt-5.6-terra` once to understand slang, jokes,
  sarcasm, and gaming tone, then Azure translates the clarified English into
  the other languages.
- All other source languages use Azure Translator directly.
- If Terra is paused, reaches CHiP's local cap, or has an API error, Spanish
  automatically falls back to Azure.
- If Azure has an API error, CHiP relays the original human message and records
  the error privately. Provider error pages are never posted into chat.
- Media-only messages never call either paid translation service.

Official setup references:

- [Azure Translator REST quickstart](https://learn.microsoft.com/en-us/azure/ai-services/translator/text-translation/quickstart/rest-api)
- [GPT-5.6 Terra model](https://developers.openai.com/api/docs/models/gpt-5.6-terra)
- [GPT-5.6 Luna model](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [OpenAI usage dashboard](https://platform.openai.com/usage)

## Railway variables

Copy every variable from `.env.example` into the Railway service's **Variables**
page. Never commit real keys or the Discord token to GitHub.

Important values:

```text
ANNOUNCEMENT_CHANNEL_ID=1529511999891964015
AZURE_TRANSLATOR_ENDPOINT=https://api.cognitive.microsofttranslator.com
OPENAI_MODEL=gpt-5.6-terra
CHIP_CHAT_MODEL=gpt-5.6-luna
TERRA_BUDGET_USD=4.50
CHIP_DATA_DIR=/data
```

For Azure, create a **Translator**, **single-service**, **global**, **F0**
resource. Copy its key into `AZURE_TRANSLATOR_KEY`. A global resource can leave
`AZURE_TRANSLATOR_REGION` blank. A regional or multi-service resource must use
the exact region shown on Azure's **Keys and Endpoint** page.

For Terra and member chat, create one dedicated OpenAI project named `CHiP`,
generate one project API key, add the initial prepaid credit, and turn automatic
recharge **OFF**. Terra uses `gpt-5.6-terra`; member chat uses the lower-cost
`gpt-5.6-luna`. Both count toward the same $4.50 cumulative CHiP safety cap.
`/terra limit` changes only that local safety cap; it cannot add credit or
charge a card.

## Persistent usage tracking on Railway

Add a Railway volume and mount it at `/data`, then set:

```text
CHIP_DATA_DIR=/data
```

Without a volume, Railway can erase the SQLite usage database during a new
deployment. With the volume, message links, maintenance state, combined AI
usage, each member's ten most recent CHiP conversation messages, contest drafts,
uploaded contest images, managers, votes, and the local budget setting survive
restarts and deployments.

## Admin slash commands

The commands sync to the Discord server containing the configured CHiP
announcement channel.

- `/maintenance start [reason]` — posts in the existing CHiP announcement
  channel, sends generic update-or-bug-fix notices to every translation
  channel, changes CHiP's status, and pauses translation relays. Add an optional
  reason when members need more detail.
- `/maintenance end` — resumes relays, restores CHiP's status, and announces
  that translation and other CHiP services are available again.
- `/maintenance status` — shows whether maintenance is active and when it began.
- `/terra status` — shows the combined tracked total, Terra/chat breakdown,
  month-to-date cost, remaining local budget, requests, and token usage.
- `/terra limit <amount_usd>` — changes CHiP's **cumulative shared** AI spending
  cap. If $4.50 has been used and another $5 is added, set the new cap to $9.50.
- `/terra pause` — switches Spanish to Azure-only.
- `/terra resume` — resumes Terra if a key and remaining local budget exist.
- `/chip status` — shows whether member chat is active, its channel, Luna usage,
  and the combined remaining AI budget.
- `/chip channel channel:<channel>` — makes a channel work like a normal CHiP
  conversation without requiring a slash command or mention.
- `/chip channel-off` — disables the dedicated chat channel while leaving
  `/chip chat` and direct @mentions available.
- `/chip pause` and `/chip resume` — control only member chat; translations and
  contests remain available.

All of these slash commands require the Discord **Administrator** permission.

## Member conversations with CHiP

Members can talk to CHiP in three ways:

- Use `/chip chat message:<question>` in any server channel.
- Directly @mention CHiP with a question in any server channel.
- Type normally in the dedicated channel selected by an administrator with
  `/chip channel`.

CHiP answers in the member's language and remembers only that member's ten most
recent user/assistant messages so follow-up questions make sense. `/chip reset`
privately clears the member's own saved conversation. Requests are limited to
1,500 characters, replies are kept short, and each member has a 15-second
cooldown to prevent spam and surprise spending.

When a conversation happens in one of the linked general translation channels,
both the member's message and CHiP's reply are mirrored into every other linked
language channel. Each copy is translated for that channel, so members following
the conversation never see only one side of it. Mirrored webhook copies are
ignored by the chat handler to prevent duplicate answers or reply loops.

Member chat uses GPT-5.6 Luna with no web-search tools and `store=False` API
requests. It does not read DMs, private messages, or hidden Discord channels.
At the shared cap, member chat stops automatically while Spanish translation
falls back to Azure. Maintenance mode pauses chat together with translation.

The normal member commands `/chip chat` and `/chip reset` do **not** require
Administrator permission.

## Recurring image contests

CHiP can stage approved profile pictures and combine up to 20 entries into each
high-resolution numbered collage. Every gallery post has matching numbered vote
buttons (five per row). Totals stay hidden, each member has one changeable vote,
and CHiP closes automatically, reveals the names, and announces the winner or
tied winners. Contests above 20 entries continue in a second gallery post. The
contest system never calls Azure or Terra.

When voting opens, CHiP posts a localized announcement with a direct contest
link in every configured general-language channel. It then sends a reminder
every 24 hours and one final reminder during the last hour. The reminders do not
ping `@everyone` and do not use Azure, Terra, or Luna.

Every contest gallery and localized announcement also includes a
**🔔 Private Reminders** button. Members must opt in themselves. CHiP first sends
an immediate confirmation DM, then privately sends the 24-hour and final-hour
reminders in the language from that member's current language role. English is
used when no language role is selected. Tapping the button again turns private
reminders off. If Discord blocks the confirmation DM, CHiP does not subscribe
the member. Closing or canceling the contest deletes all remaining opt-ins.

The Discord server owner is always a contest manager. The owner should run this
once to authorize MsMax:

```text
/contest manager-add member:@MsMax
```

Normal contest workflow:

1. `/contest create title:<title> duration_hours:<hours> anonymous:true`
2. Run `/contest add contestant_name:<name> image:<attachment>` for every
   approved image received by Nick or JC.
3. `/contest preview` privately displays the same compact collages members will
   see.
4. `/contest publish channel:<contest-channel>` posts each 20-entry collage with
   its matching numbered buttons, opens voting, and announces it in every
   general-language channel.
5. CHiP closes automatically at the deadline, or a manager can run
   `/contest close` early.

If someone was missed after publication, run `/contest add` normally while the
contest is still open. CHiP automatically rebuilds the affected collage (or
starts the next 20-entry post), adds the new numbered button, and preserves all
existing votes.

Additional management commands:

- `/contest remove entry_number:<number>` — removes an entry from a draft and
  renumbers the remaining images.
- `/contest status` — privately shows entries, vote participation, and closing
  time without exposing who is winning.
- `/contest cancel reason:<optional>` — discards a draft or ends a live contest
  without announcing any winner, rankings, names, or vote totals. Live gallery
  buttons are disabled and reminders stop immediately.
- `/contest manager-list` and `/contest manager-remove` — owner-only manager
  controls.

Only the server owner and saved contest managers can use contest-management
commands. Regular members do not submit anything to CHiP; they only press a
numbered Vote button. Uploaded contest images default to a 10 MB maximum, configurable
with `MAX_CONTEST_IMAGE_MB`.

## Emoji, sticker, GIF, and attachment relay

- Unicode emojis remain unchanged.
- Static and animated custom Discord emoji markup is protected from translation.
- GIF and other web links are protected from translation.
- Uploaded images, GIFs, and attachments are downloaded once and re-uploaded to
  each language channel when they fit the configured size limit.
- PNG, APNG, and GIF stickers are relayed as visible uploads. Lottie stickers
  use their protected Discord CDN link because they cannot be re-uploaded as a
  normal visible image.
- If a media upload fails, CHiP retries with the original protected CDN link.

CHiP needs these permissions in every translation and announcement channel:

- View Channel
- Send Messages
- Read Message History
- Add Reactions
- Manage Webhooks
- Attach Files
- Embed Links

The contest channel additionally needs **Read Message History** so CHiP can
disable old buttons and reveal results when voting closes.

The dedicated member-chat channel only needs **View Channel**, **Send Messages**,
**Read Message History**, and **Embed Links**.

## Deploy and test

1. Commit these files to the existing GitHub repository.
2. Add the Railway variables and `/data` volume.
3. Deploy or restart the Railway service.
4. Run `/terra status`, `/chip status`, and `/maintenance status` in Discord.
5. Choose a dedicated channel with `/chip channel`, then test a normal message,
   `/chip chat`, a follow-up question, and `/chip reset`.
6. Test a Spanish slang message, a custom animated emoji, a sticker, a GIF link,
   and an uploaded image in a translation channel.
7. Run `/contest manager-add member:@MsMax`, create a two-image test contest,
   vote, and close it.
8. Check Railway logs for Azure, Terra, and Luna all reporting configured.
