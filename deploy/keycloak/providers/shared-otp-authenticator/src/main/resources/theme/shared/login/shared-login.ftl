<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=true; section>
  <#if section = "header">
    ${msg("sharedLoginTitle")}
  <#elseif section = "form">
    <div class="shared-login-intro">${msg("sharedLoginIntro")}</div>
    <#-- 应用子主题定义 sharedConsentNotice 时显示条款提示，链接基于该 Client 的 Home URL。 -->
    <#if msg("sharedConsentNotice") != "sharedConsentNotice" && client?? && client.baseUrl?has_content>
      <p class="shared-login-consent">${kcSanitize(msg("sharedConsentNotice", client.baseUrl?remove_ending("/")))?no_esc}</p>
    </#if>
    <section class="shared-login-method">
      <h2>${msg("sharedPasswordTitle")}</h2>
      <form action="${url.loginAction}" method="post" class="shared-login-form">
        <input type="hidden" name="method" value="password">
        <label for="shared-identifier">${msg("sharedIdentifier")}</label>
        <input id="shared-identifier" name="identifier" type="text" autocomplete="username" required>
        <label for="shared-password">${msg("password")}</label>
        <input id="shared-password" name="password" type="password" autocomplete="current-password" required>
        <button type="submit" class="pf-v5-c-button pf-m-primary">${msg("sharedPasswordButton")}</button>
        <#if realm.resetPasswordAllowed>
          <a class="shared-login-link" href="${url.loginResetCredentialsUrl}">${msg("doForgotPassword")}</a>
        </#if>
      </form>
    </section>

    <div class="shared-login-divider"><span>${msg("sharedOr")}</span></div>

    <section class="shared-login-method">
      <h2>${msg("sharedEmailTitle")}</h2>
      <form action="${url.loginAction}" method="post" class="shared-login-form">
        <input type="hidden" name="method" value="email">
        <input type="hidden" name="step" value="request">
        <label for="shared-email">${msg("email")}</label>
        <input id="shared-email" name="identifier" type="email" autocomplete="email" required>
        <button type="submit" class="pf-v5-c-button pf-m-secondary">${msg("sharedEmailButton")}</button>
      </form>
    </section>

    <section class="shared-login-method">
      <h2>${msg("sharedSmsTitle")}</h2>
      <form action="${url.loginAction}" method="post" class="shared-login-form">
        <input type="hidden" name="method" value="sms">
        <input type="hidden" name="step" value="request">
        <label for="shared-phone">${msg("sharedPhone")}</label>
        <input id="shared-phone" name="identifier" type="tel" inputmode="tel" autocomplete="tel" placeholder="+86 138 0000 0000" required>
        <button type="submit" class="pf-v5-c-button pf-m-secondary">${msg("sharedSmsButton")}</button>
      </form>
    </section>
  <#elseif section = "info">
    <#if realm.registrationAllowed && !registrationDisabled??>
      <div class="shared-login-register">
        <span>${msg("noAccount")} <a href="${url.registrationUrl}">${msg("doRegister")}</a></span>
      </div>
    </#if>
  </#if>
</@layout.registrationLayout>
