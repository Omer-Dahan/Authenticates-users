# מחקר: איך פרויקטים אחרים מתמודדים עם בקשות הצטרפות "תקועות"

תאריך: 13.10.2026. מטרת המסמך: לבדוק איך בוטים אמיתיים אחרים פותרים (או לא פותרים) את שתי הבעיות של הבוט הזה:

1. **סטייה (drift):** מנהל מאשר או דוחה בקשה מתוך אפליקציית טלגרם, הבוט לא מקבל על זה כלום, והשורה אצלו נשארת "ממתינה".
2. **חשבונות מחוקים:** בקשה של חשבון שנמחק נשארת ברשימת הממתינים של הקבוצה, וקריאות ה-API נכשלות עליה.

שיטה: חיפוש ב-GitHub (חיפוש מאגרים וחיפוש קוד לפי שמות מתודות ומחרוזות שגיאה), שכפול וקריאה של הקוד של המאגרים הרלוונטיים, ובדיקה מול התיעוד הרשמי של Bot API ושל MTProto ב-core.telegram.org. הממצאים הגולמיים והציטוטים נמצאים ב-`/tmp/prior_art_raw.md` (מחוץ למאגר).

הערה חשובה מראש: הבוט הזה כבר עושה יותר מרוב מה שנמצא. הוא מאזין ל-`chat_member` (`bot/main.py`) וסוגר שורה כשמשתמש מצטרף (`bot/handlers/member_events.py`), מריץ סריקת התאמה עם `getChatMember`, ומנסה `banChatMember` כשה-decline נדחה עם "user is deactivated". לא נמצא אף פרויקט אחר שעושה את הנפילה הזו לחסימה.

## הפרויקטים שנבדקו

| פרויקט | כוכבים / פעילות | מה הוא | בעיה 1 (סטייה) | בעיה 2 (חשבון מחוק) |
|---|---|---|---|---|
| [iamwavecut/ngbot](https://github.com/iamwavecut/ngbot) | 60, פעיל (אוקטובר 2026) | Go, בוט שומר סף. **היחיד שנמצא שמממש guard mode אמיתי** עם Mini App | עם guard mode הבקשה מוכרעת לפני שהיא נכנסת לתור, אז אין מה שיסטה. בלי Mini App הוא עונה `queue` וחוזר לזרימה הרגילה | מתייחס ל-"USER IS DEACTIVATED" על decline כאילו ה-decline הצליח, וסוגר מקומית |
| [Divkix/Alita_Robot](https://github.com/Divkix/Alita_Robot) | 249, פעיל | Go, בוט ניהול קבוצות כללי | אין טבלת ממתינים קבועה בכלל, רק מפתח Redis ל-5 דקות. `HIDE_REQUESTER_MISSING` או `USER_ALREADY_PARTICIPANT` = "כבר טופל" | חסימה ואחריה decline, ומתעלם מ"הבקשה כבר לא קיימת" |
| [KimmyXYC/ApproveByPoll-V2](https://github.com/KimmyXYC/ApproveByPoll-V2) | 5 (הגרסה הקודמת 22), פעיל | Python, אישור הצטרפות בהצבעה | יומן פעולות עמיד. רק `HIDE_REQUESTER_MISSING` נחשב הוכחה שאין בקשה; `getChatMember` משמש רק לאישור מצב חיובי, "never infer a rejection" | מצב סיום נפרד: `closed / user_deactivated`, בלי לטעון שטלגרם ניקתה משהו |
| [Poolitzer/JoinRequestChatBot](https://github.com/Poolitzer/JoinRequestChatBot) | 16, 2025 (מתחזק של python-telegram-bot) | Python, מעביר שיחה בין מבקש למנהלים | טיימר 24 שעות לכל בקשה, ואז decline. `Hide_requester_missing` = "מישהו כבר טיפל" | בולע `Forbidden` באישור ו-`Participant_id_invalid` בחסימה, וממשיך הלאה |
| [xditya/ChannelActionsBot](https://github.com/xditya/ChannelActionsBot) | 126, פעיל | grammY, אישור/דחייה אוטומטיים | מכריע מיד בתוך ה-handler, ולכן אין מצב "ממתין" שיכול לסטות | בולע כל 400/403 בשקט |
| [TelechaBot/Kitan](https://github.com/TelechaBot/Kitan) | 52, 2024 | Python, captcha לבקשות הצטרפות | תור עם תאריך תפוגה, לולאה כל 2 שניות, decline בתפוגה | מוחק את הרשומה מהתור **גם כשה-decline נכשל** |
| [WireCatLabs/tg-cli](https://github.com/WireCatLabs/tg-cli) | 12, פעיל | CLI על **חשבון משתמש** (mtcute) | קורא את רשימת הממתינים האמיתית מטלגרם, כולל מספר כולל | לא רלוונטי |
| [rishabhops/CipherElite](https://github.com/rishabhops/CipherElite) | 277, פעיל | Telethon userbot + בוט עוזר | מנסה לקרוא את רשימת הממתינים, אבל מתוך סשן **בוט** (ראו "לא עובד") | לא מטפל |
| [libnyanpasu/nyanpasu-group-bot](https://github.com/libnyanpasu/nyanpasu-group-bot) | 0, פעיל | Cloudflare Workers | אם מגיע `query_id` הוא עונה `queue` ומזהיר "configure ordinary join approval mode" | לא מטפל |
| [J-Rios/TLG_JoinCaptchaBot](https://github.com/J-Rios/TLG_JoinCaptchaBot) | 620, פעיל | captcha ותיק | **לא מטפל בבקשות הצטרפות בכלל**, רק בהצטרפות ישירה דרך `chat_member` | לא רלוונטי |
| [umputun/tg-spam](https://github.com/umputun/tg-spam) (452), [1inch/shieldy](https://github.com/1inch/shieldy) (958) | פעילים | אנטי-ספאם | אין טיפול ב-`chat_join_request` מחוץ לספריות | לא רלוונטי |

מסקנה מהטבלה: הבוטים הגדולים (tg-spam, shieldy, JoinCaptchaBot) בכלל לא מנהלים בקשות הצטרפות. אלה שכן מנהלים, כמעט כולם **לא מחזיקים טבלת ממתינים כמקור אמת**: או שהם מכריעים מיד (ChannelActionsBot), או שהם מחזיקים מצב קצר-מועד עם תפוגה (Alita, Poolitzer, Kitan). אף אחד מהם לא מציג למנהל "כמה בקשות ממתינות".

## טכניקות ששווה להעתיק

### 1. מספר הממתינים האמיתי מגיע מטלגרם, לא מהטבלה (MTProto)

זו הטכניקה היחידה שנמצאה שמוסרת את בעיה 1 מהשורש, כי היא קוראת את הרשימה שהאפליקציה מציגה.

- `messages.getChatInviteImporters` עם הדגל `requested` מחזיר את הבקשות הממתינות בפועל ומספר כולל. בשימוש ב-`WireCatLabs/tg-cli`, קובץ `src/telegram/join-requests.ts` (הפונקציה `joinRequestsOf`, ההערה בקוד: "`total` counts every pending request, not just this page").
- `channelFull.requests_pending` ו-`recent_requesters` (שדות של `channels.getFullChannel`) נותנים את המספר בלבד. בשימוש ב-`teamofnepal448/devil-tracker-27`, קובץ `bot.py` שורות 156-165.
- **מגבלה, מאומתת מהתיעוד הרשמי:** ב-`getChatInviteImporters` כתוב "Only users can use this method". כלומר צריך **חשבון משתמש** שהוא מנהל בקבוצה (api_id, api_hash, התחברות עם טלפון, session), לא טוקן של בוט. שני הפרויקטים שלמעלה רצים על חשבון משתמש.
- `channels.getFullChannel` כן מסומן "Both users and bots can use this method", אבל **לא אומת** אם השרת ממלא את `requests_pending` עבור בוט. זה הניסוי הזול והחשוב ביותר שאפשר לעשות: להתחבר עם Pyrogram או Telethon בטוקן של הבוט הקיים (רק api_id ו-api_hash, בלי חשבון משתמש), לקרוא `full_chat.requests_pending` בקבוצה אחת ולהשוות למספר באפליקציה. אם זה עובד, הבוט יכול להציג את המספר האמיתי ליד המספר שלו.

### 2. מצב סיום נפרד וכן לחשבון מושבת

מקור: `KimmyXYC/ApproveByPoll-V2`, קובץ `app/recovery_operations.py`.

- שורות 25-35: סיווג מדויק בלבד. קוד 400 או 403, ותיאור (אחרי הסרת "bad request: "/"forbidden: ") ששווה בדיוק לאחד מ: `user is deactivated`, `input_user_deactivated`, `user_deactivated`.
- שורות 110-121: `close_deactivated` שומר קודם את ההוכחה ורק אחר כך סוגר את המשימה כ-`closed / user_deactivated`, עם ההערה "Persist proof first ... without calling Telegram or claiming approval success".
- שורות 278-280: "Only this explicit Telegram error proves that there is no pending request. A left member or arbitrary 400 does not."

הבוט הזה כבר עושה דבר דומה (`unresolvable`, `cleared_via_ban`). מה שכדאי לקחת: את רשימת המחרוזות המלאה (כולל `INPUT_USER_DEACTIVATED` של MTProto), ואת הכלל שאישור דרך `getChatMember` מאשר רק מצב חיובי ולעולם לא מסיק דחייה.

### 3. `USER_ALREADY_PARTICIPANT` הוא אות לאישור שנעשה באפליקציה

מקור: `Divkix/Alita_Robot`, קובץ `alita/modules/greetings.go` שורות 1039-1047 (`isJoinRequestGone`), ו-`WireCatLabs/tg-cli`, `src/telegram/join-requests.ts` (`USER_ALREADY_PARTICIPANT` מחזיר `already: true`).

בבוט הזה אין אף התייחסות ל-`USER_ALREADY_PARTICIPANT` בקוד. כשמנהל לוחץ "אשר" בתפריט של הבוט על בקשה שכבר אושרה באפליקציה, `bot/handlers/settings_menu.py` (סביב שורה 721) יציג שגיאה במקום לסגור את השורה כ"אושר מחוץ לבוט". בנוסף, כש-approve מחזיר `HIDE_REQUESTER_MISSING` השורה נרשמת כ-`rejected`, אף שייתכן שהיא אושרה באפליקציה; עדיף לבדוק `getChatMember` באותו רגע ולהבחין בין השניים. (ממצא מהקוד של הבוט הזה, לא נבדק מול טלגרם חי.)

### 4. כל שורה ממתינה מקבלת תאריך תפוגה, וה-decline בתפוגה הוא ה"בדיקה"

מקור: `Poolitzer/JoinRequestChatBot`, `bot.py` שורות 127-160 (טיימר 24 שעות, ואז decline; `Hide_requester_missing` פירושו "someone already took care of that join request"). דומה ב-`TelechaBot/Kitan`, `backend/bot/controller.py` שורות 671-719.

ההיגיון: אין דרך לבוט לשאול "האם הבקשה עדיין ממתינה" בלי לשנות אותה. decline הוא הבדיקה היחידה שמבחינה בין "עדיין ממתינה" (מצליח) לבין "מנהל כבר טיפל באפליקציה" (`HIDE_REQUESTER_MISSING`). מכיוון שזו בדיקה הורסת, מותר להשתמש בה רק על בקשות שהבוט ממילא היה דוחה. אצלנו `reconciliation_stale_days` כבוי כברירת מחדל (0); ההמלצה היא להפעיל אותו עם ערך מפורש, כי זה המנגנון היחיד שסוגר בסוף שורות של דחיות שנעשו באפליקציה.

### 5. guard mode עובד גם עם long polling ושרת HTTPS עצמאי

מקור: `iamwavecut/ngbot`.

- `internal/handlers/chat/gatekeeper_join_processor.go` שורות 317-420: אם יש `query_id` וכתובת Mini App מוגדרת, קורא ל-`startJoinRequestWebAppChallenge`; אחרת שומר שורת "גבול" במסד, עונה `queue` וממשיך בזרימה הרגילה.
- `internal/handlers/chat/gatekeeper_webapp.go` שורות 794-870: יוצר אתגר במסד עם `JoinRequestQueryID` וטוקן, ואז `sendChatJoinRequestWebApp`.
- `internal/handlers/chat/gatekeeper_challenge_service.go` שורה 577: `answerChatJoinRequestQuery(approve)` ומעבר למצב "ממתין להצטרפות" עם TTL; שורה 715: `answerChatJoinRequestQuery(decline)`.
- `cmd/ngbot/main.go` שורה 120: הבוט רץ ב-**long polling**. ה-Mini App מוגש משרת מובנה מאחורי Caddy (`NG_GATEKEEPER_WEBAPP_PUBLIC_URL` ב-README).

המשמעות ל-`docs/GUARD-MODE.md`: guard mode **לא מחייב** מעבר ל-Telegram Serverless ולא מחייב webhook. הבוט ב-Python יכול להמשיך ב-polling ולהריץ לצדו שרת HTTPS קטן (למשל aiohttp) שמגיש את ה-Mini App ומאמת `initData`. זה מבטל את ההנחה במסמך שאין נתיב חלקי.

## מה נראה טוב אבל לא עובד

1. **לקרוא את רשימת הממתינים מסשן של בוט.** `rishabhops/CipherElite`, `bot_plugins/joinmod.py` שורות 162-171, קורא ל-`GetChatInviteImportersRequest(requested=True)` מתוך לקוח שהופעל עם `bot.start(bot_token=...)` (`plugins/bot.py` שורה 252), ו-`except Exception: return None`. לפי התיעוד הרשמי המתודה "Only users can use this method", כך שהתוצאה למשתמש היא "Nobody is waiting for approval" גם כשיש ממתינים. אותו דבר ל-`hideAllChatJoinRequests`.
2. **להתייחס ל-"user is deactivated" על decline כהצלחה.** `iamwavecut/ngbot`, `gatekeeper_challenge_service.go` (`isTelegramJoinDeclineAlreadyApplied`) כולל את `USER IS DEACTIVATED` ברשימת "כבר בוצע". ההוכחה החיה ב-`docs/DELETED-REQUEST-REMOVAL.md` (636 מקרים) מראה שה-decline נכשל בזמן שהבקשה עדיין ממתינה. זה מסתיר את השורה אצל הבוט ומשאיר את הבקשה באפליקציה, כלומר בדיוק הפער שהבעלים רואה, בכיוון ההפוך.
3. **למחוק את הרשומה המקומית בלי לבדוק את תוצאת ה-decline.** `TelechaBot/Kitan`, `controller.py` שורה 718. זה הבאג שתוקן כאן ב-`verification/cleanup.py`.
4. **לזהות דחייה שנעשתה באפליקציה דרך `getChatMember`.** משתמש שבקשתו ממתינה ומשתמש שבקשתו נדחתה מחזירים שניהם `left`. אי אפשר להבחין ביניהם. (נגזר מהסמנטיקה של ה-API; אף פרויקט לא טוען אחרת.)
5. **לחכות לעדכון `chat_member` על דחייה.** דחייה לא משנה את סטטוס המשתמש בקבוצה, ולכן אין `ChatMemberUpdated`. עדכון כן מגיע על **אישור** (עם `via_join_request`), והבוט הזה כבר משתמש בו.
6. **להשוות `getChatMemberCount` לאורך זמן.** נותן רק מספר, בלי לדעת מי הצטרף ודרך מה. לא נמצא פרויקט שמשתמש בזה להתאמה.
7. **guard mode שעונה `queue`.** `libnyanpasu/nyanpasu-group-bot` (`src/lib/service.ts` שורות 326-333) ו-ngbot (במצב בלי Mini App) עונים `queue`. אחרי `queue` הבקשה היא בקשה ממתינה רגילה, ומנהל יכול לטפל בה באפליקציה בדיוק כמו היום, כך שבעיה 1 חוזרת במלואה.
8. **חסימה ואז ביטול חסימה לחשבון שנמחק כליל.** אם `banChatMember` עצמו נכשל עם `PARTICIPANT_ID_INVALID`, אין מה לבטל. לא נמצא אף פרויקט שעושה ban ואז unban כדי לנקות בקשה. `J-Rios/TLG_JoinCaptchaBot` (`src/tlgbotutils.py` שורות 410-435) משתמש ב-`unbanChatMember(only_if_banned=False)` רק כ"הוצאה בלי חסימה" של חבר קיים, לא לבקשות.

## על guard mode (Bot API 10.0): מה הוא באמת דורש ומה הוא נותן

מאומת מהתיעוד הרשמי (`core.telegram.org/bots/api`, 13.10.2026):

- `ChatJoinRequest.query_id` מגיע רק לבוט שהוגדר לעבד את הבקשות, וחייבים לענות תוך **10 שניות** ב-`sendChatJoinRequestWebApp` או ב-`answerChatJoinRequestQuery`.
- `answerChatJoinRequestQuery`: `approve`, `decline`, או `queue` ("to leave the decision to other administrators").
- `sendChatJoinRequestWebApp` מקבל כתובת **HTTPS**, ואחרי האינטראקציה קוראים ל-`answerChatJoinRequestQuery`.
- `ChatFullInfo.guard_bot` גלוי למנהלים בלבד; `User.supports_join_request_queries` חוזר רק ב-`getMe`.

מה הוא נותן לשתי הבעיות:

- **בעיה 1:** אם הבוט עונה `approve` או `decline` (ישירות או אחרי ה-Mini App) ולא `queue`, הבקשה מוכרעת לפני שהיא יושבת בתור של המנהלים, ולכן אין מה שיסטה. זה הפתרון המבני היחיד שנמצא, והוא עובד רק לבקשות חדשות, לא למה שכבר תקוע.
- **בעיה 2:** בקשה שמוכרעת תוך שניות או דקות לא מספיקה "להזדקן" עד שהחשבון נמחק. זה מצמצם מאוד את הבעיה בבקשות חדשות, אבל לא פותר את המלאי הקיים.
- **שימוש בפועל:** מחוץ לספריות (aiogram, grammY, Pyrogram forks ועוד שכבר תומכות במתודות), נמצאו רק שני יישומים: ngbot שמממש את זה באמת, ו-nyanpasu שעונה `queue` ובורח. זו תכונה חדשה מאוד (מאי 2026) עם אימוץ קטן.

## מה שאף פרויקט לא פותר, כי ה-API לא מאפשר

את זה כדאי לומר לבעלים במפורש, בלי הבטחה לתיקון:

1. **ל-Bot API אין שום דרך לקבל את רשימת הבקשות הממתינות או את מספרן.** לא `getChat`, לא `ChatFullInfo`, לא עדכון. הרשימה זמינה רק ל**חשבון משתמש** דרך MTProto. כל מספר "ממתינים" שבוט מציג מתוך הטבלה שלו הוא הערכה, לא עובדה.
2. **דחייה שנעשתה באפליקציה לא משאירה שום סימן שבוט יכול לקרוא** עד שהבוט עצמו מנסה decline ומקבל `HIDE_REQUESTER_MISSING`. אישור באפליקציה כן נראה (עדכון `chat_member`, או `getChatMember` שמחזיר member).
3. **חשבון שטלגרם מחקה כליל** (`PARTICIPANT_ID_INVALID` / `USER_ID_INVALID`): כל קריאה של בוט נכשלת, כולל חסימה. אף פרויקט שנבדק לא מצליח להסיר בקשה כזו. מה שהם עושים: Poolitzer ו-ChannelActionsBot בולעים את השגיאה, ApproveByPoll סוגר מקומית עם סיבה מפורשת, ngbot סוגר מקומית כאילו הצליח.
4. **עדכונים נשמרים אצל טלגרם עד 24 שעות.** מה שקרה בזמן שהבוט היה למטה יותר מזה, אבוד.
5. **אין דרך לא-הורסת לבדוק אם בקשה עדיין ממתינה.** הבדיקה היחידה היא לנסות לאשר או לדחות אותה.

המסקנה המעשית: הטבלה המקומית היא **יומן ביקורת** של מה שהבוט ראה ועשה, לא ספירה חיה. ההמלצה היא שהתצוגה למנהל תאמר את זה (למשל "פתוחות אצל הבוט" ולא "ממתינות בקבוצה"), ושאם הניסוי בסעיף 1 מצליח, יוצג לצדה המספר של טלגרם.

## לא אומת

דברים שנגזרו מתיעוד, מ-README או מקריאת קוד בלי הרצה, ולא נבדקו מול טלגרם חי:

- האם `channels.getFullChannel` דרך **סשן בוט** מחזיר את `requests_pending` מלא, או שהשרת מסתיר אותו מבוטים. התיעוד רק אומר שבוטים רשאים לקרוא למתודה.
- האם `getChatInviteImporters(requested)` מחשבון משתמש מחזיר גם בקשות של חשבונות מחוקים, ובאיזו צורה.
- האם `hideAllChatJoinRequests` (decline לכולם, חשבון משתמש בלבד) מנקה גם בקשות של חשבונות שנמחקו כליל, כשהקריאה הפרטנית נכשלת עליהם. זה המועמד היחיד שנמצא לבעיה 2 במקרה הקשה, והוא לא נבדק בשום מקום.
- שהבקשה נעלמת מרשימת הממתינים אחרי `banChatMember` מוצלח על חשבון מושבת (גם `docs/DELETED-REQUEST-REMOVAL.md` מסמן את זה כהנחה).
- ש-Bot API מחזיר בדיוק `USER_ALREADY_PARTICIPANT` כש-approve נקרא על מי שכבר חבר. נלקח מהקוד של Alita (Bot API דרך gotgbot) ומהתיעוד של MTProto, לא מבדיקה.
- האם בוט שאינו ה-guard bot עדיין מקבל `chat_join_request` כשה-guard bot עונה. לא מתועד, ו-ngbot לא מתייחס לזה.
- מה קורה כשמבקש פותח את ה-Mini App ונוטש: האם ה-`query_id` נשאר תקף לשעות, והאם הבקשה מופיעה למנהלים באפליקציה בזמן הזה. ngbot מניח שמותר לענות decline בסוף זמן האתגר; התיעוד לא אומר כמה זמן מותר.
- `tlgrcli/tlgr` (193 כוכבים) נמצא בחיפוש כקורא `requests_pending` ו-`getChatInviteImporters`; הקוד שלו לא נקרא.
- מגבלת מספר החברים שבוט יכול למשוך דרך `channels.getParticipants` לא נבדקה (התיעוד אומר שבוטים רשאים; ידוע שיש תקרה בצד השרת).
- הקביעה ש"tg-spam ו-shieldy לא מטפלים בבקשות הצטרפות" מבוססת על חיפוש מחרוזות בקוד ששוכפל, לא על קריאה מלאה.
