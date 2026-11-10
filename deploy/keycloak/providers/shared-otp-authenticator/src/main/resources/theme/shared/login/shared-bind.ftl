<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=true; section>
  <#if section = "header">
    <#if channel = "email">${msg("sharedBindEmailTitle")}<#else>${msg("sharedBindPhoneTitle")}</#if>
  <#elseif section = "form">
    <p class="shared-login-intro">
      <#if maskedCurrent??>${msg("sharedBindCurrent", maskedCurrent)}<#else>${msg("sharedBindNone")}</#if>
    </p>
    <form action="${url.loginAction}" method="post" class="shared-login-form">
      <input type="hidden" name="step" value="request">
      <#if channel = "email">
        <label for="shared-bind-identifier">${msg("sharedBindNewEmail")}</label>
        <input id="shared-bind-identifier" name="identifier" type="email" autocomplete="email" required autofocus>
      <#else>
        <label for="shared-bind-identifier">${msg("sharedBindNewPhone")}</label>
        <input id="shared-bind-identifier" name="identifier" type="tel" inputmode="tel" autocomplete="tel" placeholder="+86 138 0000 0000" required autofocus>
      </#if>
      <button type="submit" class="pf-v5-c-button pf-m-primary">${msg("sharedBindSendCode")}</button>
    </form>
    <#if isAppInitiatedAction??>
      <form action="${url.loginAction}" method="post" class="shared-login-resend">
        <button type="submit" name="cancel-aia" value="true" class="pf-v5-c-button pf-m-link" formnovalidate>${msg("doCancel")}</button>
      </form>
    </#if>
  </#if>
</@layout.registrationLayout>
