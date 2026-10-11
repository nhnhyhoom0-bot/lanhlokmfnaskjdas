# بناء xlop-launcher.exe مستقل (لا يحتاج Python عند المستخدم)

لا يمكن بناء ملف exe مستقل إلا على Windows (أو على خدمة بناء Windows). لديك طريقتان:

## الطريقة 1 — GitHub (موصى بها، لا تحتاج جهاز Windows)
1. أنشئ مستودعًا على GitHub وارفع إليه محتويات هذه الحزمة (مع المجلد `.github`).
2. من تبويب **Actions** اختر **Build Windows exe** ثم **Run workflow**.
3. بعد دقائق نزّل `xlop-launcher.exe` من قسم **Artifacts**.
4. لإصدار رسمي: أنشئ وسم (tag) مثل `v1.2.0` فيُرفق الملف تلقائيًا بالـ Release.

## الطريقة 2 — على جهاز Windows
ثبّت Python من python.org ثم انقر مرتين على `build-exe.bat`. تجد الناتج في `dist\xlop-launcher.exe`.

## بعد البناء
- ضع `microsoft_client_id.txt` و`update_url.txt` بجوار الـ exe (أو شغّل البناء بعد تعبئتهما).
- التحديثات: أنشئ ملف latest.json بالأداة `tools/make_update_manifest.py` مع `--windows-url رابط_الـexe`.
- ملف exe غير موقّع رقميًا، لذلك قد تعرض Windows تحذير SmartScreen عند أول تشغيل ("More info" ثم "Run anyway").
