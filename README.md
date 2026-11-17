# CHiP Bot

CHiP is CMON's Discord translation, announcements, member-chat, and role bot. This
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

Translation channels are isolated by group: general-language chat, private R4
chat, and optional SvS channels. After creating SvS channels in Discord, set
`SVS_ENGLISH_CHANNEL_ID`, `SVS_SPANISH_CHANNEL_ID`, and
`SVS_PORTUGUESE_CHANNEL_ID` in CHiP's Railway Variables. Any unset IDs are
ignored; for two-way SvS translation set at least two IDs. Restart/redeploy
CHiP after updating variables. Ensure CHiP has View Channel, Send Messages,
Manage Webhooks, and Attach Files in each channel. SvS messages never mirror
to general or R4 channels.

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
usage, each member's ten most recent CHiP conversation messages, archived
legacy CHiP contest data, and the local budget setting survive
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
  announcements remain available.

These admin groups require the Discord **Administrator** permission. The
`/announcement` command below is restricted to the Discord server owner.

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

## Owner announcements with optional images

Use `/announcement` for a single multilingual embed in CHiP's configured
announcement channel. Text is translated into English, Spanish, and Portuguese.
The command accepts:

- `message` — required English announcement (maximum 1,500 characters;
  translated embed fields must each fit Discord's 1,024-character limit).
- `title` — optional heading.
- `image` — optional PNG, JPG, GIF, or WebP image attached directly in Discord.
  CHiP uploads the image with the finished embed, rather than relying on an
  expiring URL. Default maximum 10 MB, configurable with
  `MAX_ANNOUNCEMENT_IMAGE_MB` in Railway.
- `publish` — `false` privately previews the message and image (default);
  `true` publishes. Attach the image again when running the publishing command.

For a Den voting graphic, enter the officer's voting text, select the graphic
under `image`, preview with `publish:false`, then run it again with
`publish:true`. Announcements without images work exactly as before.

## Event ownership: BiSCUiT

BiSCUiT is the **only** bot that hosts PFP contests and other managed
competition events. CHiP's former `/contest` command group is no longer
registered, and CHiP does not restore old voting buttons, send event reminders,
or automatically close and announce old contests.

Legacy CHiP contest records are deliberately **not deleted** from the existing
SQLite database. Before migrating any unfinished CHiP-run contest, close or
cancel it through the original deployment, or begin a fresh contest under
BiSCUiT. Do not assume old CHiP votes automatically transfer to BiSCUiT.

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
7. Preview `/announcement` with and without an image, then publish an image
   to the announcement channel. Verify all three languages and the image.
8. Verify CHiP no longer offers `/contest`, and use BiSCUiT for PFP contests.
9. With the SvS channel IDs configured, send a test message in SvS English and
   check Spanish and Portuguese receive only their SvS copies.
10. Check Railway logs for Azure, Terra, and Luna all reporting configured.


## Server redesign lockdown (separate from bot maintenance)

Owner-only commands: `/lockdown status`, `/lockdown start`, `/lockdown end`.
`/lockdown start` and `/lockdown end` default to preview-only; specify `confirm:true` to apply.
The start command saves original channel/category permission overwrites, sync state, and names in a persistent JSON backup under `CHIP_DATA_DIR`, then hides every channel except the CHiP announcement channel (renamed `🔧server-maintenance`, read-only). End restores all saved state.
**Prerequisites:** Railway persistent volume mounted at `/data`, `CHIP_DATA_DIR=/data`, CHiP Administrator permission, and no other Administrator roles accessible to other members. Administrator bypasses channel overrides; review all roles, including any role shared with CHiP. Do not remove the Railway volume while locked.
**Caution:** This feature has not been live-tested on your Discord server. Test on a separate test server first. Channel/role additions and permission changes made while locked may not be restored. If a restore fails, the backup is retained; rerun end or restore manually from JSON. Existing `/maintenance` commands remain unchanged.
