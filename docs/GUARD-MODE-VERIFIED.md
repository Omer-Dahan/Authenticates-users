# מצב Guard Bot (שאילתות בקשות הצטרפות): אימות מול מקורות ראשוניים

תאריך בדיקה: 2026-10-13. הציטוטים נשלפו ב-curl מהעמודים הרשמיים. הקבצים הגולמיים וכל הציטוטים המלאים נמצאים ב-`/tmp/guard_mode_raw.md`.

## תקציר

- **שאלת ה-polling: כן, זה עובד ב-long polling.** אין עדכון חדש. ה-`query_id` מגיע כשדה בתוך העדכון הרגיל `chat_join_request`, שהבוט כבר מקבל היום. אין צורך ב-webhook ואין ערך חדש ב-`allowed_updates`. ngbot עושה בדיוק את זה ב-getUpdates.
- **Mini App לא חובה.** מותר לענות ישירות ב-`answerChatJoinRequestQuery` תוך 10 שניות.
- **אבל guard mode לא מעלים את ה-drift אצלנו.** רק תשובות `approve` ו-`decline` סוגרות את הבקשה דרך הבוט. התשובה `queue` מחזירה את ההחלטה למנהלים, כלומר הבקשה נשארת ברשימה הרגילה והמנהל יכול לאשר או לדחות אותה באפליקציה בלי שהבוט ידע. זרימת האימות של הבוט שלנו נמשכת הרבה יותר מ-10 שניות, ולכן רוב הבקשות יגיעו ל-`queue` וה-drift יישאר בדיוק כמו היום.
- הדרך היחידה לסגור בקשה ארוכה דרך הבוט היא Mini App (`sendChatJoinRequestWebApp`), וזה דורש שרת HTTPS ציבורי עם דומיין. זה שינוי אירוח של ממש.
- **לא מצאתי בשום מקור רשמי איך בעל הקבוצה מגדיר guard bot** (BotFather, הגדרות הקבוצה, או משהו אחר). זה לא מאומת.

## 1. מה אומר התיעוד הרשמי

### גרסה
התכונה נוספה ב-**Bot API 10.1 מ-11 ביוני 2026** (לא ב-10.0). מקור: https://core.telegram.org/bots/api, יומן השינויים, תת-כותרת "Join Request Queries":

> Added the field supports_join_request_queries to the class User.
> Added the field guard_bot to the class ChatFullInfo.
> Added the field query_id to the class ChatJoinRequest.
> Added the method answerChatJoinRequestQuery.
> Added the method sendChatJoinRequestWebApp.

### איזה עדכון נושא את השאילתה
אין סוג עדכון חדש. השדה יושב בתוך `ChatJoinRequest`, שמגיע בעדכון `chat_join_request` (https://core.telegram.org/bots/api#chatjoinrequest):

> query_id | String | Optional. Identifier of the join request query; for bots assigned to process join requests only. If present, then the bot must call sendChatJoinRequestWebApp or directly call answerChatJoinRequestQuery within 10 seconds.

ומעמוד ה-Mini Apps (https://core.telegram.org/bots/webapps, "Launching Mini Apps on chat join requests"):

> When a user joins a chat in which the bot was set up as a guard bot, the bot receives an Update “chat_join_request” with the field query_id.

תנאי קבלת העדכון עצמו (https://core.telegram.org/bots/api#update):

> chat_join_request | ChatJoinRequest | Optional. A request to join the chat has been sent. The bot must have the can_invite_users administrator right in the chat to receive these updates.

### הדדליין
**10 שניות.** הציטוט למעלה: "must call sendChatJoinRequestWebApp or directly call answerChatJoinRequestQuery within 10 seconds".

### answerChatJoinRequestQuery
https://core.telegram.org/bots/api#answerchatjoinrequestquery

> Use this method to process a received chat join request query. Returns True on success.

| פרמטר | סוג | חובה | תיאור (מצוטט) |
|---|---|---|---|
| `chat_join_request_query_id` | String | Yes | Unique identifier of the join request query |
| `result` | String | Yes | Result of the query. Must be either “approve” to allow the user to join the chat, “decline” to disallow the user to join the chat, or “queue” to leave the decision to other administrators. |

### sendChatJoinRequestWebApp, והאם Mini App חובה
https://core.telegram.org/bots/api#sendchatjoinrequestwebapp

> Use this method to process a received chat join request query by showing a Mini App to the user before deciding the outcome. Call answerChatJoinRequestQuery to resolve the join request query based on the user interaction with the Mini App. Returns True on success.

| פרמטר | סוג | חובה | תיאור (מצוטט) |
|---|---|---|---|
| `chat_join_request_query_id` | String | Yes | Unique identifier of the join request query |
| `web_app_url` | String | Yes | An HTTPS URL of a Web App to be opened with additional data as specified in Initializing Web Apps |

**Mini App לא חובה.** הניסוח "sendChatJoinRequestWebApp **or directly call** answerChatJoinRequestQuery" ועמוד ה-Mini Apps אומר "The bot **may** call the method sendChatJoinRequestWebApp". אבל אם רוצים Mini App, הכתובת חייבת להיות HTTPS.

### guard_bot ו-supports_join_request_queries
https://core.telegram.org/bots/api#user

> supports_join_request_queries | Boolean | Optional. True, if the bot supports join request queries and can be assigned to process them. Returned only in getMe.

https://core.telegram.org/bots/api#chatfullinfo

> guard_bot | User | Optional. The bot that processes join request queries in the chat. The field is only available to chat administrators.

### איפה בעל הקבוצה מגדיר את זה
חיפשתי "guard", "assigned", "join request quer" בעמודים: `bots/api`, `bots/api-changelog`, `bots/webapps`, `bots/serverless`, `bots/features`, `bots/faq`. **אין שום הוראה איך משייכים guard bot לקבוצה**, לא ב-BotFather, לא בהגדרות הקבוצה ולא ב-API (אין מתודה `setChatGuardBot` או דומה). גם ה-README של ngbot לא מזכיר שלב כזה. ראו סעיף "לא מאומת".

### Serverless
https://core.telegram.org/bots/serverless לא מזכיר guard bots או join request queries בכלל. Serverless לא נדרש.

## 2. שאלת ה-polling: תשובה חד-משמעית

**כן, long polling מקבל את השאילתות.** הנימוק:

1. השאילתה איננה סוג עדכון נפרד. היא השדה `query_id` בתוך `chat_join_request`. התיעוד קובע ש-getUpdates ו-webhook הם "two mutually exclusive ways of receiving updates", ושום מקום לא מגביל את `chat_join_request` ל-webhook.
2. `allowed_updates` ב-getUpdates (מצוטט): "Specify an empty list to receive all update types except chat_member, message_reaction, and message_reaction_count (default)". `chat_join_request` כלול בברירת המחדל.
3. **הערך הנדרש ב-`allowed_updates`: `"chat_join_request"` בלבד, והוא כבר קיים** ב-`bot/main.py:112`. אין מה להוסיף.
4. ראיה בקוד: ngbot רץ ב-long polling (`cmd/ngbot/main.go:120`, `internal/bot/polling.go:49` עוטף את `GetUpdatesWithContext`), עם `AllowedUpdates` שכולל `"chat_join_request"` (`cmd/ngbot/main.go:583-593`), וקורא ל-`AnswerJoinRequestQuery` עם ה-`QueryID` שהגיע באותו עדכון (`internal/handlers/chat/gatekeeper_join_processor.go:378`).

הסתייגות אחת: את מסירת `query_id` בפועל ב-polling לא הרצתי בעצמי מול קבוצה אמיתית. המסקנה נשענת על התיעוד ועל ngbot, שזה הדפוס שבו הוא בנוי.

## 3. מימוש אמיתי: ngbot

מאגר: https://github.com/iamwavecut/ngbot, commit `259c55ea` מ-2026-10-05, ספריית Go `github.com/OvyFlash/telegram-bot-api` (`go.mod:6`).

| שאלה | מה ngbot עושה | איפה |
|---|---|---|
| polling או webhook | long polling ב-getUpdates | `cmd/ngbot/main.go:120`, `internal/bot/polling.go:49` |
| allowed_updates | כולל `chat_join_request`, `chat_member`, `my_chat_member` | `cmd/ngbot/main.go:586-592` |
| איך נרשם כ-guard bot | **לא נרשם בקוד.** אין קריאה לשום מתודת רישום. ה-README מבקש רק ליצור בוט ב-BotFather ולתת הרשאות Ban, Delete, Invite | `README.md:37`, `README.md:61` |
| Mini App חובה? | לא. רק אם הוגדר `NG_GATEKEEPER_WEBAPP_PUBLIC_URL` (HTTPS בלבד, http נדחה) | `gatekeeper_join_processor.go:331-333`, `internal/config/config_test.go:250`, `README.md:60` |
| עם Mini App | `sendChatJoinRequestWebApp`, ואחרי הצלחה `answer...("approve")`, אחרי כישלון `"decline"` | `gatekeeper_webapp.go:853`, `gatekeeper_challenge_service.go:577`, `:715` |
| בלי Mini App | עונה **מיד** `"queue"`, ואז ממשיך את המודרציה הרגילה (בדיקת באן, קפצ'ה ב-DM) ומחליט בסוף ב-`approveChatJoinRequest`/`declineChatJoinRequest` הרגילים | `gatekeeper_join_processor.go:348-378`, `internal/bot/update_processor.go:336`, `:353` |
| משתמש חסום ידוע | `"decline"` ישירות דרך השאילתה | `gatekeeper_join_processor.go:716` |
| שאילתה שלא נענתה | ngbot לא מטפל בזה במפורש. הוא כותב שורת "boundary" ל-DB לפני הקריאה ומבצע reconciliation אם הקריאה נכשלה, אבל אין בקוד הנחה על מה טלגרם עושה אחרי 10 שניות | `gatekeeper_join_processor.go:348-400` |

המסקנה מ-ngbot: הדפוס המעשי בלי Mini App הוא "queue מיד, החלט אחר כך בדרך הרגילה". זה בדיוק המצב שיש לנו היום.

### ספריות Python
- **aiogram**: תמיכה מלאה מגרסה **3.29.0 (2026-06-14)**: `AnswerChatJoinRequestQuery`, `SendChatJoinRequestWebApp`, השדה `ChatJoinRequest.query_id`, וקיצורים `ChatJoinRequest.answer_query()` ו-`send_webapp()`. הגרסה האחרונה 3.31.0. **הפרויקט נעול על `aiogram==3.13.1`**, שאין בה כלום מזה.
- **python-telegram-bot**: הגרסה האחרונה v22.8 (2026-06-12). חיפוש קוד במאגר אחרי `answer_chat_join_request_query` החזיר 0, ול-`ChatJoinRequest` ב-master אין `query_id`. אין תמיכה.
- לא מצאתי בוט Python ציבורי שמממש guard mode.

## 4. ההשלכה על הפרויקט הזה

**האם הרשימה הרגילה של בקשות ממתינות עדיין קיימת?** לפי הנוסח של `queue` ("to leave the decision to other administrators"), בקשה שנענתה `queue` עוברת להחלטת המנהלים, כלומר לרשימה הרגילה. ngbot מסתמך על זה: אחרי `queue` הוא מאשר או דוחה ב-`approveChatJoinRequest` הרגיל, שעובד רק על בקשה שעדיין ממתינה. בקשה שנענתה `approve` או `decline` מוכרעת מיד ולא נשארת ממתינה.

**האם guard mode מעלים את המקרה "מנהל אישר או דחה באפליקציה בלי שהבוט ידע"?** **לא. רק עבור בקשות שהבוט מכריע בעצמו תוך 10 שניות.**

- הבוט שלנו מכריע מיד רק במקרים מעטים: חסימות, rate limit, raid (`bot/handlers/join_requests.py:266`, `:375`). שם guard mode כן סוגר את הבקשה בלי drift.
- אבל זרימת האימות (`bot/handlers/verification.py`) ואישור ידני מהתפריט (`bot/handlers/settings_menu.py:723`, `:734`) נמשכים דקות עד שעות. לבקשות אלו אין ברירה אלא `queue`, הן חוזרות לרשימה של המנהלים, והמנהל יכול לפעול עליהן באפליקציה בלי שהבוט יקבל עדכון. **זה בדיוק ה-drift הנוכחי.**
- בקשות שנשלחו לפני שהבוט הוגדר כ-guard, ובקשות שהגיעו כשהבוט היה למטה ולא ענה תוך 10 שניות, לא מכוסות. מה טלגרם עושה איתן לא מתועד.
- כדי שבקשה ארוכה תיסגר דרך הבוט צריך Mini App: האימות קורה בתוך ה-Mini App והבוט עונה `approve`/`decline`. גם אז לא מתועד אם הבקשה נראית למנהל ברשימה בזמן שה-Mini App פתוח.

## 5. מחיר אימוץ

| רכיב | בלי Mini App | עם Mini App |
|---|---|---|
| ספרייה | שדרוג aiogram 3.13.1 ל-3.29.0 לפחות (מומלץ 3.31.0). קפיצה של 16 גרסאות מינוריות, דורש סבב בדיקות | אותו דבר |
| קוד | ענף חדש ב-handler של `chat_join_request`: אם יש `query_id`, להכריע או לענות `queue` תוך 10 שניות, לפני כל עבודה איטית. לשמור את התשובה ב-DB למניעת כפילויות | בנוסף: שרת HTTP בתוך התהליך, דף קפצ'ה, אימות `initData` (HMAC), חיבור התשובה לשאילתה |
| אירוח | ללא שינוי. polling, systemd | דומיין ציבורי, תעודת TLS, reverse proxy (ngbot משתמש ב-Caddy), פורט פתוח. סותר את "אין שרת web" |
| מאמץ הבעלים בקבוצה | לשייך את הבוט כ-guard bot. **המקום שבו עושים זאת לא מתועד** | אותו דבר |
| רווח | אין drift רק לבקשות שנדחות מיד | אין drift לכל בקשה שעוברת דרך ה-Mini App |

## 6. הרצפה: מה כן עובד ב-polling בלבד

guard mode בלי Mini App לא פותר את הבעיה, ועם Mini App הוא דורש שרת HTTPS. החלופה הטובה ביותר ב-polling בלבד, ללא userbot:

1. **להמשיך עם ה-reconciliation sweep הקיים** (commit `869d639`): `get_chat_member` לכל שורה ממתינה. מי שהפך לחבר אושר, ומי שעדיין לא חבר נשאר ממתין או נדחה.
2. **להאזין ל-`chat_member` עם `via_join_request`** (כבר ב-`allowed_updates`) כדי לסגור מיד שורה של מי שאושר באפליקציה.
3. **דחייה באפליקציה היא המקרה היחיד שאי אפשר לזהות** ב-Bot API. הדרך הזולה לזהות אותה: לנסות `approve`/`decline` רגיל כשהבוט מחליט, ולתפוס `HIDE_REQUESTER_MISSING`, שמשמעותו שהבקשה כבר טופלה.
4. אם המספר האמיתי נדרש: סשן **בוט** ב-MTProto קורא `ChannelFull.requests_pending` (נמדד קודם), כבדיקת סכום בלבד.
5. אפשרות ביניים זולה: לשדרג aiogram ולהשתמש ב-guard mode **רק** לדחייה מיידית (חסום או raid) ולענות `queue` לכל השאר. זה לא מזיק, אבל גם לא פותר את ה-drift. לא מומלץ כעבודה בפני עצמה עד שיתברר איך משייכים guard bot.

## 7. טענות שלא הצלחתי לאמת

- **[לא מאומת] איך ואיפה משייכים guard bot לקבוצה.** אין הוראה באף עמוד רשמי שנבדק, אין מתודת API לכך, ו-ngbot לא מתעד שלב כזה. סביר שזה בהגדרות הקבוצה באפליקציה, אבל זו השערה.
- **[לא מאומת] איך בוט מקבל `supports_join_request_queries=true`.** "Returned only in getMe" מרמז על הגדרה ברמת הבוט (כנראה ב-BotFather), אבל זה לא כתוב.
- **[לא מאומת] מה קורה לשאילתה שלא נענתה תוך 10 שניות** (אישור אוטומטי, דחייה, או מעבר לרשימה הרגילה כמו `queue`). לא מתועד, ו-ngbot לא מניח שום דבר בעניין.
- **[לא מאומת] האם בקשה נראית ברשימת הממתינים של המנהל בזמן ה-10 שניות או בזמן שה-Mini App פתוח.**
- **[מוסק, לא נבדק בפועל] שבקשה שנענתה `queue` מופיעה ברשימה הרגילה ואפשר לאשר אותה באפליקציה.** נשען על הנוסח "leave the decision to other administrators" ועל כך ש-ngbot מאשר אותה אחר כך ב-`approveChatJoinRequest` הרגיל.
- **[מוסק, לא נבדק בפועל] שה-`query_id` מגיע ב-getUpdates.** נשען על התיעוד ועל המבנה של ngbot. לא הרצתי זאת מול קבוצה.
- **[לא מאומת] האם guard bot חייב להיות גם מנהל עם `can_invite_users`.** התיעוד דורש זאת לקבלת `chat_join_request` באופן כללי, ולא אומר אם guard bot פטור.
