// Страница перехода в Робокассу: отправить подписанную форму, не дожидаясь клика.
// Отдельным файлом, а не в разметке: политика безопасности запрещает встроенные скрипты.
document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("robokassa");
  if (form) form.submit();
});
