(function(){
  "use strict";
  var form=document.getElementById("loginForm"),message=document.getElementById("loginMessage"),button=document.getElementById("loginButton");
  async function request(url,options){
    var response=await fetch(url,Object.assign({headers:{"Content-Type":"application/json"}},options||{}));
    var data=await response.json();
    if(!response.ok)throw new Error(data.message||data.error||("HTTP "+response.status));
    return data;
  }
  form.addEventListener("submit",async function(event){
    event.preventDefault();
    message.textContent="";
    button.disabled=true;
    button.textContent="正在验证…";
    try{
      var next=new URLSearchParams(window.location.search).get("next")||"";
      var data=await request("/api/auth/login",{method:"POST",body:JSON.stringify({username:document.getElementById("username").value,password:document.getElementById("password").value,next:next})});
      window.location.replace(data.redirect||"/portal");
    }catch(error){
      message.textContent=error.message;
      document.getElementById("password").select();
    }finally{
      button.disabled=false;
      button.textContent="登录演示系统";
    }
  });
})();
