// Stripe Payment Element on the booking page (loaded only when a payment is
// pending and the provider has a browser-side step). Follows Stripe's
// intent-first Payment Element integration: mount on the PaymentIntent's
// client secret, confirm in the browser, let Stripe redirect back. The
// booking settles by webhook (payment_intent.succeeded); nothing here is
// trusted as proof of payment.
(function () {
  "use strict";

  // Stripe appends payment_intent, payment_intent_client_secret and
  // redirect_status to the return URL. Drop them from the address bar so the
  // secret is not left in history or copied with the link.
  if (window.location.search) {
    var params = new URLSearchParams(window.location.search);
    var stripeParams = ["payment_intent", "payment_intent_client_secret", "redirect_status"];
    var changed = false;
    stripeParams.forEach(function (name) {
      if (params.has(name)) {
        params.delete(name);
        changed = true;
      }
    });
    if (changed) {
      var rest = params.toString();
      window.history.replaceState(null, "", window.location.pathname + (rest ? "?" + rest : ""));
    }
  }

  var form = document.getElementById("payment-form");
  if (!form || typeof window.Stripe !== "function") {
    return;
  }
  var message = document.getElementById("payment-message");
  var submit = document.getElementById("payment-submit");
  var stripe = window.Stripe(form.dataset.publishableKey);
  var elements = stripe.elements({ clientSecret: form.dataset.clientSecret });
  elements.create("payment", { layout: "accordion" }).mount("#payment-element");

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    submit.disabled = true;
    message.textContent = "";
    var result = await stripe.confirmPayment({
      elements: elements,
      confirmParams: { return_url: form.dataset.returnUrl },
    });
    // Only reached on an immediate error (for example incomplete card
    // details); on success Stripe redirects to the return URL.
    if (result.error) {
      message.textContent = result.error.message || "The payment could not be completed.";
      submit.disabled = false;
    }
  });
})();
