<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=true displayInfo=true; section>
  <#if section = "header">
    ${msg("sharedLoginTitle")}
  <#elseif section = "form">
    <#-- 同名 details 互斥展开：先显示登录方式按钮，点开后才出现对应表单。 -->
    <div class="shared-login-methods">
      <details class="shared-login-method" name="shared-login-method">
        <summary>
          <svg class="shared-login-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>
          <span>${msg("sharedEmailTitle")}</span>
        </summary>
        <form action="${url.loginAction}" method="post" class="shared-login-form">
          <input type="hidden" name="method" value="email">
          <input type="hidden" name="step" value="request">
          <label for="shared-email">${msg("email")}</label>
          <input id="shared-email" name="identifier" type="email" autocomplete="email" required>
          <button type="submit" class="pf-v5-c-button pf-m-primary">${msg("sharedEmailButton")}</button>
        </form>
      </details>

      <details class="shared-login-method" name="shared-login-method">
        <summary>
          <svg class="shared-login-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="2.5" width="12" height="19" rx="2"/><path d="M10.5 18h3"/></svg>
          <span>${msg("sharedSmsTitle")}</span>
        </summary>
        <form action="${url.loginAction}" method="post" class="shared-login-form">
          <input type="hidden" name="method" value="sms">
          <input type="hidden" name="step" value="request">
          <label for="shared-phone">${msg("sharedPhone")}</label>
          <input id="shared-phone" name="identifier" type="tel" inputmode="tel" autocomplete="tel" placeholder="+86 138 0000 0000" required>
          <button type="submit" class="pf-v5-c-button pf-m-primary">${msg("sharedSmsButton")}</button>
        </form>
      </details>

      <details class="shared-login-method" name="shared-login-method">
        <summary>
          <svg class="shared-login-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/></svg>
          <span>${msg("sharedPasswordTitle")}</span>
        </summary>
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
      </details>
    </div>
  <#elseif section = "info">
    <#if realm.registrationAllowed && !registrationDisabled??>
      <p class="shared-login-register">${msg("noAccount")} <a href="${url.registrationUrl}">${msg("doRegister")}</a></p>
    </#if>
    <#-- 应用子主题定义 sharedConsentNotice 时显示条款提示，链接基于该 Client 的 Home URL。 -->
    <#if msg("sharedConsentNotice") != "sharedConsentNotice" && client?? && client.baseUrl?has_content>
      <p class="shared-login-consent">${kcSanitize(msg("sharedConsentNotice", client.baseUrl?remove_ending("/")))?no_esc}</p>
    </#if>
  </#if>
</@layout.registrationLayout>
