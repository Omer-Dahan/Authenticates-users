import urllib.request
import urllib.parse
import json

def create_telegraph_page():
    # 1. Create account
    account_url = "https://api.telegra.ph/createAccount?short_name=ModerationFAQ&author_name=ModerationSystem"
    try:
        req = urllib.request.Request(account_url, method="GET")
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if not res_data.get("ok"):
                print("Failed to create Telegraph account:", res_data)
                return
            access_token = res_data["result"]["access_token"]
    except Exception as e:
        print("Error creating account:", e)
        return

    # 2. Prepare content nodes
    # Telegraph page content must be a JSON array of Node objects
    content = [
        {"tag": "p", "children": [{"tag": "strong", "children": ["מערכת סינון ובקרה להצטרפות לקבוצות טלגרם — מדריך למשתמש ושאלות נפוצות"]}]},
        {"tag": "p", "children": ["בוט זה פועל כשומר סף (Gatekeeper) לקבוצות הטלגרם שלך. הוא נועד להגן על הקבוצה מפני בוטים, ספאמרים וחשבונות פיקטיביים, על ידי ניתוח פרטי המשתמשים המצטרפים, תוך שימוש בכללים מותאמים אישית, סינון שפות ושאלות אימות."]},
        
        {"tag": "h3", "children": ["📋 איך המערכת עובדת?"]},
        {"tag": "ul", "children": [
            {"tag": "li", "children": ["משתמש שולח בקשת הצטרפות לקבוצה."]},
            {"tag": "li", "children": ["הבוט קולט את הבקשה ומנתח את פרטי המשתמש: שם פרטי, שם משפחה ושם משתמש (Username)."]},
            {"tag": "li", "children": ["הפרטים נבדקים מול כללים שהגדרת, רשימות שחורות ולבנות, שפות מזוהות והתאמה לשמות ישראליים."]},
            {"tag": "li", "children": ["בהתאם לבדיקות, מחושב למשתמש ציון (Score) שמושווה לספי הניקוד שהגדרת."]},
            {"tag": "li", "children": ["במידה ומופעל מצב אבטחה 'קפדני' (Strict) או שהציון מחייב אימות, הבוט ישלח למשתמש שאלת אימות בהודעה פרטית."]},
            {"tag": "li", "children": ["לסיום, מתקבלת החלטה אוטומטית: אישור, דחייה, חסימה או העברה לסקירה ידנית על ידי המנהלים (Review Queue)."]}
        ]},

        {"tag": "h3", "children": ["🛡️ מצבי אבטחה (Security Modes)"]},
        {"tag": "p", "children": ["המערכת מציעה שלושה מצבי אבטחה לבחירתך:"]},
        {"tag": "ul", "children": [
            {"tag": "li", "children": [{"tag": "strong", "children": ["🟢 רגיל (Normal):"]}, " סינון חכם המבוסס על ניקוד בלבד. משתמשים עם ציון גבוה מאושרים אוטומטית, ואילו אלו עם ציון נמוך נדרשים לעבור אימות או מועברים לסקירה של המנהלים."]},
            {"tag": "li", "children": [{"tag": "strong", "children": ["🟡 קפדני (Strict):"]}, " כל מצטרף חדש מחויב לענות על שאלת אימות כדי להיכנס לקבוצה, ללא קשר לציון שקיבל."]},
            {"tag": "li", "children": [{"tag": "strong", "children": ["🔴 נעילה (Lockdown):"]}, " כל בקשות ההצטרפות מועברות ישירות לסקירה ידנית של מנהלים (או נדחות אוטומטית), ללא שום אישור אוטומטי."]}
        ]},

        {"tag": "h3", "children": ["❓ שאלות נפוצות (FAQ)"]},

        {"tag": "p", "children": [{"tag": "strong", "children": ["שאלה: האם הבוט יכול למנוע ספאם באופן אקטיבי מתוך הקבוצה?"]}]},
        {"tag": "p", "children": [{"tag": "em", "children": ["תשובה: נכון לעכשיו, הבוט מסנן משתמשים רק בשלב הכניסה (בקשות הצטרפות). הוא אינו עוקב אחרי הודעות בקבוצה ולכן אינו מוחק ספאם שנשלח על ידי חברים קיימים."]}]},
        {"tag": "br", "children": []},

        {"tag": "p", "children": [{"tag": "strong", "children": ["שאלה: איך עובד מנגנון שאלות האימות (Verification)?"]}]},
        {"tag": "p", "children": [{"tag": "em", "children": ["תשובה: אם משתמש צריך לעבור אימות, הבוט ישלח לו בפרטי שאלת אימות אקראית שבחרת מראש. המשתמש יצטרך לענות נכון במסגרת הזמן ומספר הניסיונות שהגדרת (למשל: 3 ניסיונות בתוך 5 דקות) כדי להיות מאושר."]}]},
        {"tag": "br", "children": []},

        {"tag": "p", "children": [{"tag": "strong", "children": ["שאלה: מה קורה אם משתמש נכשל באימות או מתעלם ממנו?"]}]},
        {"tag": "p", "children": [{"tag": "em", "children": ["תשובה: משתמש שטועה יותר ממספר הניסיונות המותר, או שלא עונה בזמן, יידחה באופן אוטומטי. בנוסף, תוכל להגדיר חסימה אוטומטית למשתמשים שנכשלו בשאלות מסוימות."]}]},
        {"tag": "br", "children": []},

        {"tag": "p", "children": [{"tag": "strong", "children": ["שאלה: מהי בדיקת ההתאמה לשמות (Fuzzy Name Matching)?"]}]},
        {"tag": "p", "children": [{"tag": "em", "children": ["תשובה: הבוט משווה את שמו של המשתמש למאגר שמות ישראליים נפוצים. אם רמת ההתאמה (לדוגמה 80%) גבוהה מספיק, המשתמש יקבל תוספת ניקוד שתעזור לו להיכנס לקבוצה בקלות רבה יותר."]}]},
        {"tag": "br", "children": []},

        {"tag": "p", "children": [{"tag": "strong", "children": ["שאלה: האם משתמש יוכל להיכנס אם הוא הוזמן לקבוצה על ידי חבר?"]}]},
        {"tag": "p", "children": [{"tag": "em", "children": ["תשובה: לא. כל עוד הקבוצה מוגדרת כך שנדרש אישור מנהל להצטרפות, כל בקשה עוברת דרך הבוט. כדי לעקוף זאת, מנהל יצטרך להוסיף את המשתמש בעצמו או להכניס אותו ל'רשימה הלבנה' (Whitelist)."]}]},
        {"tag": "br", "children": []},

        {"tag": "p", "children": [{"tag": "strong", "children": ["שאלה: האם הבוט יכול לנהל מספר קבוצות במקביל?"]}]},
        {"tag": "p", "children": [{"tag": "em", "children": ["תשובה: בהחלט. המערכת נועדה לנהל קבוצות רבות במקביל. תוכלו לחבר מספר קבוצות לאותו הבוט ולשלוט בכל קבוצה בנפרד (כללים, רשימות, שאלות ועוד), דרך לוח הבקרה או בצ'אט מול הבוט."]}]},
        {"tag": "br", "children": []},

        {"tag": "p", "children": [{"tag": "strong", "children": ["שאלה: איך מנהלים יכולים לאשר משתמשים שנשלחו לסקירה ידנית?"]}]},
        {"tag": "p", "children": [{"tag": "em", "children": ["תשובה: המנהלים יכולים להיכנס לתור הסקירה (Review Queue) בלוח הבקרה באינטרנט, או לאשר, לדחות ולחסום משתמשים בקלות באמצעות כפתורים שהבוט שולח להם בהודעה פרטית."]}]}
    ]

    # 3. Create Page
    create_page_url = "https://api.telegra.ph/createPage"
    params = {
        "access_token": access_token,
        "title": "מדריך למשתמש ושאלות נפוצות - בוט סינון הצטרפות",
        "author_name": "מערכת מודרציה",
        "content": json.dumps(content),
        "return_content": "false"
    }

    try:
        data = urllib.parse.urlencode(params).encode('utf-8')
        req = urllib.request.Request(create_page_url, data=data, method="POST")
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if not res_data.get("ok"):
                print("Failed to create Telegraph page:", res_data)
                return
            page_url = res_data["result"]["url"]
            print("\n" + "="*50)
            print("Telegraph page created successfully!")
            print("URL:", page_url)
            print("="*50 + "\n")
            return page_url
    except Exception as e:
        print("Error creating page:", e)
        return

if __name__ == "__main__":
    create_telegraph_page()
