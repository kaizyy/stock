package nl.valerith.stockroom;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.os.Message;
import android.view.Gravity;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

public final class MainActivity extends Activity {
    private static final int FILE_REQUEST=41,CAMERA_REQUEST=42;
    private WebView webView; private ProgressBar progress; private LinearLayout errorView; private Uri appUri;
    private ValueCallback<Uri[]> fileCallback; private PermissionRequest webPermission;

    @Override protected void onCreate(Bundle state){super.onCreate(state);appUri=Uri.parse(BuildConfig.STOCKROOM_BASE_URL);createContent();if(state==null||webView.restoreState(state)==null)webView.loadUrl(appUri+"/");}
    private void createContent(){FrameLayout root=new FrameLayout(this);webView=new WebView(this);progress=new ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal);progress.setMax(100);progress.setVisibility(View.GONE);errorView=createErrorView();errorView.setVisibility(View.GONE);root.addView(webView,new FrameLayout.LayoutParams(-1,-1));root.addView(errorView,new FrameLayout.LayoutParams(-1,-1));root.addView(progress,new FrameLayout.LayoutParams(-1,dp(3)));setContentView(root);configureWebView();}
    private LinearLayout createErrorView(){LinearLayout box=new LinearLayout(this);box.setOrientation(LinearLayout.VERTICAL);box.setGravity(Gravity.CENTER);box.setPadding(dp(28),dp(28),dp(28),dp(28));box.setBackgroundColor(0xfff5f7f4);TextView title=new TextView(this);title.setText("Stockroom kan niet worden geladen");title.setTextSize(20);title.setTextColor(0xff173f32);title.setGravity(Gravity.CENTER);TextView help=new TextView(this);help.setText("Controleer je internetverbinding en probeer opnieuw.");help.setGravity(Gravity.CENTER);help.setPadding(0,dp(10),0,dp(18));Button retry=new Button(this);retry.setText("Opnieuw proberen");retry.setOnClickListener(v->{errorView.setVisibility(View.GONE);webView.reload();});box.addView(title);box.addView(help);box.addView(retry);return box;}
    private void configureWebView(){WebSettings s=webView.getSettings();s.setJavaScriptEnabled(true);s.setDomStorageEnabled(true);s.setDatabaseEnabled(true);s.setAllowFileAccess(false);s.setAllowContentAccess(true);s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);s.setSupportZoom(false);s.setMediaPlaybackRequiresUserGesture(false);s.setUserAgentString(s.getUserAgentString()+" StockroomAndroid/2.0");CookieManager cookies=CookieManager.getInstance();cookies.setAcceptCookie(true);cookies.setAcceptThirdPartyCookies(webView,false);WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);WebView.startSafeBrowsing(this,null);webView.setWebChromeClient(new StockroomChromeClient());webView.setWebViewClient(new StockroomWebClient());}

    private final class StockroomChromeClient extends WebChromeClient{
        @Override public void onProgressChanged(WebView view,int value){progress.setProgress(value);progress.setVisibility(value>=100?View.GONE:View.VISIBLE);}
        @Override public boolean onShowFileChooser(WebView view,ValueCallback<Uri[]> callback,FileChooserParams params){if(fileCallback!=null)fileCallback.onReceiveValue(null);fileCallback=callback;Intent intent=params.createIntent();intent.addCategory(Intent.CATEGORY_OPENABLE);intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);try{startActivityForResult(intent,FILE_REQUEST);return true;}catch(Exception ex){fileCallback=null;return false;}}
        @Override public void onPermissionRequest(PermissionRequest request){runOnUiThread(()->{if(!isTrusted(request.getOrigin())||!contains(request.getResources(),PermissionRequest.RESOURCE_VIDEO_CAPTURE)){request.deny();return;}if(checkSelfPermission(Manifest.permission.CAMERA)==PackageManager.PERMISSION_GRANTED)request.grant(new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});else{webPermission=request;requestPermissions(new String[]{Manifest.permission.CAMERA},CAMERA_REQUEST);}});}
        @Override public boolean onCreateWindow(WebView view,boolean dialog,boolean gesture,Message resultMsg){WebView popup=new WebView(MainActivity.this);popup.setWebViewClient(new WebViewClient(){@Override public boolean shouldOverrideUrlLoading(WebView ignored,WebResourceRequest request){openPopup(request.getUrl());return true;}@Override public void onPageStarted(WebView ignored,String url,Bitmap icon){openPopup(Uri.parse(url));popup.stopLoading();}});((WebView.WebViewTransport)resultMsg.obj).setWebView(popup);resultMsg.sendToTarget();return true;}
    }
    private final class StockroomWebClient extends WebViewClient{
        @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request){return handleNavigation(request.getUrl());}
        @Override public boolean shouldOverrideUrlLoading(WebView view,String url){return handleNavigation(Uri.parse(url));}
        @Override public void onPageStarted(WebView view,String url,Bitmap icon){progress.setVisibility(View.VISIBLE);errorView.setVisibility(View.GONE);}
        @Override public void onPageFinished(WebView view,String url){progress.setVisibility(View.GONE);CookieManager.getInstance().flush();}
        @Override public void onReceivedError(WebView view,WebResourceRequest request,WebResourceError error){if(request.isForMainFrame())errorView.setVisibility(View.VISIBLE);}
        @Override public void onReceivedSslError(WebView view,SslErrorHandler handler,SslError error){handler.cancel();errorView.setVisibility(View.VISIBLE);}
    }
    private void openPopup(Uri uri){if(isTrusted(uri))webView.loadUrl(uri.toString());else launchExternal(uri);}
    private boolean handleNavigation(Uri uri){if(isTrusted(uri))return false;launchExternal(uri);return true;}
    private boolean isTrusted(Uri uri){String scheme=uri==null?null:uri.getScheme(),host=uri==null?null:uri.getHost();return "https".equalsIgnoreCase(scheme)&&host!=null&&host.equalsIgnoreCase(appUri.getHost());}
    private void launchExternal(Uri uri){try{startActivity(new Intent(Intent.ACTION_VIEW,uri));}catch(Exception ignored){}}
    private boolean contains(String[] values,String target){for(String value:values)if(target.equals(value))return true;return false;}
    @Override protected void onActivityResult(int code,int result,Intent data){super.onActivityResult(code,result,data);if(code==FILE_REQUEST&&fileCallback!=null){fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result,data));fileCallback=null;}}
    @Override public void onRequestPermissionsResult(int code,String[] permissions,int[] results){super.onRequestPermissionsResult(code,permissions,results);if(code==CAMERA_REQUEST&&webPermission!=null){if(results.length>0&&results[0]==PackageManager.PERMISSION_GRANTED)webPermission.grant(new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});else webPermission.deny();webPermission=null;}}
    @Override protected void onSaveInstanceState(Bundle state){webView.saveState(state);super.onSaveInstanceState(state);}
    @Override public void onBackPressed(){if(errorView.getVisibility()==View.VISIBLE){errorView.setVisibility(View.GONE);webView.reload();}else if(webView.canGoBack())webView.goBack();else super.onBackPressed();}
    @Override protected void onDestroy(){if(fileCallback!=null)fileCallback.onReceiveValue(null);if(webPermission!=null)webPermission.deny();webView.stopLoading();webView.setWebChromeClient(null);webView.setWebViewClient(null);webView.destroy();super.onDestroy();}
    private int dp(int value){return Math.round(value*getResources().getDisplayMetrics().density);}
}
