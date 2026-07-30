# webroot_deploy/

Справочные копии того, что реально задеплоено на nginx вне репозитория
(`/var/www/html/`). Git не может тронуть /var/www/html — путь физически
вне дерева репозитория, git pull его не затронет.

## help.html
Живёт по адресу: /var/www/html/help.html
Открывается на: https://support.pchelp-24.com/support

После изменения боевого файла — обновите копию здесь:
    cp -a /var/www/html/help.html webroot_deploy/help.html
    git add webroot_deploy/help.html
    git commit -m "Обновлена копия help.html для истории"
