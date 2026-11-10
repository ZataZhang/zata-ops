<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=true; section>
  <#if section = "header">
    <#if channel = "email">${msg("sharedEmailCodeTitle")}<#else>${msg("sharedSmsCodeTitle")}</#if>
  <#elseif section = "form">
    <p class="shared-login-intro">${msg("sharedCodeSentTo", maskedRecipient)}</p>
    <form action="${url.loginAction}" method="post" class="shared-login-form">
      <input type="hidden" name="method" value="${channel}">
      <input type="hidden" name="step" value="verify">
      <label for="shared-code">${msg("sharedCodeLabel")}</label>
      <input id="shared-code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required autofocus>
      <button type="submit" class="pf-v5-c-button pf-m-primary">${msg("sharedVerifyCode")}</button>
    </form>
    <form action="${url.loginAction}" method="post" class="shared-login-resend">
      <input type="hidden" name="method" value="${channel}">
      <input type="hidden" name="step" value="request">
      <button type="submit" class="pf-v5-c-button pf-m-link">${msg("sharedResendCode")}</button>
      <a class="shared-login-link" href="${url.loginRestartFlowUrl}">${msg("sharedChooseAnotherMethod")}</a>
    </form>
  </#if>
</@layout.registrationLayout>
