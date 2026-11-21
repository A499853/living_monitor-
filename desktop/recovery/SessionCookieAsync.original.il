	.class nested private auto ansi sealed beforefieldinit '<SessionCookieAsync>d__25'
		extends [mscorlib]System.ValueType
		implements [mscorlib]System.Runtime.CompilerServices.IAsyncStateMachine
	{
		.custom instance void [mscorlib]System.Runtime.CompilerServices.CompilerGeneratedAttribute::.ctor() = (
			01 00 00 00
		)
		// Fields
		.field public int32 '<>1__state'
		.field public valuetype [mscorlib]System.Runtime.CompilerServices.AsyncTaskMethodBuilder`1<string> '<>t__builder'
		.field public class DouyinWindow '<>4__this'
		.field public string '<url>5__26'
		.field public class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie> '<cookies>5__27'
		.field public class DouyinWindow/'<>c__DisplayClass23' 'CS$<>8__locals24'
		.field public string[] '<>7__wrap28'
		.field public int32 '<>7__wrap29'
		.field private valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>> '<>u__$awaiter2a'
		.field private object '<>t__stack'

		// Methods
		.method private final hidebysig newslot virtual 
			instance void MoveNext () cil managed 
		{
			.override method instance void [mscorlib]System.Runtime.CompilerServices.IAsyncStateMachine::MoveNext()
			// Method begins at RVA 0x57c8
			// Header size: 12
			// Code size: 682 (0x2aa)
			.maxstack 4
			.locals init (
				[0] class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie,
				[1] bool,
				[2] string,
				[3] class [mscorlib]System.Exception,
				[4] int32,
				[5] string[],
				[6] valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>>,
				[7] valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>>,
				[8] class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>,
				[9] valuetype [mscorlib]System.Collections.Generic.List`1/Enumerator<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>,
				[10] bool
			)

			.try
			{
				IL_0000: ldc.i4.1
				IL_0001: stloc.1
				IL_0002: ldarg.0
				IL_0003: ldfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>1__state'
				IL_0008: stloc.s 4
				IL_000a: ldloc.s 4
				IL_000c: ldc.i4.0
				IL_000d: beq.s IL_0011

				IL_000f: br.s IL_0013

				IL_0011: br.s IL_0033

				IL_0013: br.s IL_0015

				IL_0015: nop
				IL_0016: ldarg.0
				IL_0017: newobj instance void DouyinWindow/'<>c__DisplayClass23'::.ctor()
				IL_001c: stfld class DouyinWindow/'<>c__DisplayClass23' DouyinWindow/'<SessionCookieAsync>d__25'::'CS$<>8__locals24'
				IL_0021: nop
				IL_0022: ldarg.0
				IL_0023: ldfld class DouyinWindow/'<>c__DisplayClass23' DouyinWindow/'<SessionCookieAsync>d__25'::'CS$<>8__locals24'
				IL_0028: newobj instance void class [mscorlib]System.Collections.Generic.Dictionary`2<string, string>::.ctor()
				IL_002d: stfld class [mscorlib]System.Collections.Generic.Dictionary`2<string, string> DouyinWindow/'<>c__DisplayClass23'::values
				IL_0032: nop

				IL_0033: ldarg.0
				IL_0034: ldfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>1__state'
				IL_0039: stloc.s 4
				IL_003b: ldloc.s 4
				IL_003d: ldc.i4.0
				IL_003e: beq.s IL_0042

				IL_0040: br.s IL_0047

				IL_0042: br IL_00e0

				IL_0047: br.s IL_0049

				IL_0049: ldarg.0
				IL_004a: ldc.i4.2
				IL_004b: newarr [mscorlib]System.String
				IL_0050: stloc.s 5
				IL_0052: ldloc.s 5
				IL_0054: ldc.i4.0
				IL_0055: ldstr "https://www.douyin.com/"
				IL_005a: stelem.ref
				IL_005b: ldloc.s 5
				IL_005d: ldc.i4.1
				IL_005e: ldstr "https://live.douyin.com/"
				IL_0063: stelem.ref
				IL_0064: ldloc.s 5
				IL_0066: stfld string[] DouyinWindow/'<SessionCookieAsync>d__25'::'<>7__wrap28'
				IL_006b: ldarg.0
				IL_006c: ldc.i4.0
				IL_006d: stfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>7__wrap29'
				IL_0072: br IL_01cf

				IL_0077: ldarg.0
				IL_0078: ldarg.0
				IL_0079: ldfld string[] DouyinWindow/'<SessionCookieAsync>d__25'::'<>7__wrap28'
				IL_007e: ldarg.0
				IL_007f: ldfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>7__wrap29'
				IL_0084: ldelem.ref
				IL_0085: stfld string DouyinWindow/'<SessionCookieAsync>d__25'::'<url>5__26'
				IL_008a: nop
				IL_008b: ldarg.0
				IL_008c: ldfld class DouyinWindow DouyinWindow/'<SessionCookieAsync>d__25'::'<>4__this'
				IL_0091: ldfld class [Microsoft.Web.WebView2.WinForms]Microsoft.Web.WebView2.WinForms.WebView2 DouyinWindow::browser
				IL_0096: callvirt instance class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2 [Microsoft.Web.WebView2.WinForms]Microsoft.Web.WebView2.WinForms.WebView2::get_CoreWebView2()
				IL_009b: callvirt instance class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2CookieManager [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2::get_CookieManager()
				IL_00a0: ldarg.0
				IL_00a1: ldfld string DouyinWindow/'<SessionCookieAsync>d__25'::'<url>5__26'
				IL_00a6: callvirt instance class [mscorlib]System.Threading.Tasks.Task`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>> [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2CookieManager::GetCookiesAsync(string)
				IL_00ab: callvirt instance valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<!0> class [mscorlib]System.Threading.Tasks.Task`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>>::GetAwaiter()
				IL_00b0: stloc.s 6
				IL_00b2: ldloca.s 6
				IL_00b4: call instance bool valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>>::get_IsCompleted()
				IL_00b9: brtrue.s IL_00ff

				IL_00bb: ldarg.0
				IL_00bc: ldc.i4.0
				IL_00bd: stfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>1__state'
				IL_00c2: ldarg.0
				IL_00c3: ldloc.s 6
				IL_00c5: stfld valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>> DouyinWindow/'<SessionCookieAsync>d__25'::'<>u__$awaiter2a'
				IL_00ca: ldarg.0
				IL_00cb: ldflda valuetype [mscorlib]System.Runtime.CompilerServices.AsyncTaskMethodBuilder`1<string> DouyinWindow/'<SessionCookieAsync>d__25'::'<>t__builder'
				IL_00d0: ldloca.s 6
				IL_00d2: ldarg.0
				IL_00d3: call instance void valuetype [mscorlib]System.Runtime.CompilerServices.AsyncTaskMethodBuilder`1<string>::AwaitUnsafeOnCompleted<valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>>, valuetype DouyinWindow/'<SessionCookieAsync>d__25'>(!!0&, !!1&)
				IL_00d8: nop
				IL_00d9: ldc.i4.0
				IL_00da: stloc.1
				IL_00db: leave IL_02a8

				IL_00e0: ldarg.0
				IL_00e1: ldfld valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>> DouyinWindow/'<SessionCookieAsync>d__25'::'<>u__$awaiter2a'
				IL_00e6: stloc.s 6
				IL_00e8: ldarg.0
				IL_00e9: ldloca.s 7
				IL_00eb: initobj valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>>
				IL_00f1: ldloc.s 7
				IL_00f3: stfld valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>> DouyinWindow/'<SessionCookieAsync>d__25'::'<>u__$awaiter2a'
				IL_00f8: ldarg.0
				IL_00f9: ldc.i4.m1
				IL_00fa: stfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>1__state'

				IL_00ff: ldloca.s 6
				IL_0101: call instance !0 valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>>::GetResult()
				IL_0106: ldloca.s 6
				IL_0108: initobj valuetype [mscorlib]System.Runtime.CompilerServices.TaskAwaiter`1<class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>>
				IL_010e: stloc.s 8
				IL_0110: ldarg.0
				IL_0111: ldloc.s 8
				IL_0113: stfld class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie> DouyinWindow/'<SessionCookieAsync>d__25'::'<cookies>5__27'
				IL_0118: nop
				IL_0119: ldarg.0
				IL_011a: ldfld class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie> DouyinWindow/'<SessionCookieAsync>d__25'::'<cookies>5__27'
				IL_011f: callvirt instance valuetype [mscorlib]System.Collections.Generic.List`1/Enumerator<!0> class [mscorlib]System.Collections.Generic.List`1<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>::GetEnumerator()
				IL_0124: stloc.s 9
				.try
				{
					IL_0126: br.s IL_0128

					IL_0128: br.s IL_012a

					IL_012a: br.s IL_0197
					// loop start (head: IL_0197)
						IL_012c: ldloca.s 9
						IL_012e: call instance !0 valuetype [mscorlib]System.Collections.Generic.List`1/Enumerator<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>::get_Current()
						IL_0133: stloc.0
						IL_0134: ldloc.0
						IL_0135: callvirt instance bool [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie::get_IsSession()
						IL_013a: brtrue.s IL_014e

						IL_013c: ldloc.0
						IL_013d: callvirt instance valuetype [mscorlib]System.DateTime [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie::get_Expires()
						IL_0142: call valuetype [mscorlib]System.DateTime [mscorlib]System.DateTime::get_UtcNow()
						IL_0147: call bool [mscorlib]System.DateTime::op_GreaterThan(valuetype [mscorlib]System.DateTime, valuetype [mscorlib]System.DateTime)
						IL_014c: brfalse.s IL_0172

						IL_014e: ldloc.0
						IL_014f: callvirt instance string [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie::get_Name()
						IL_0154: ldstr ";"
						IL_0159: callvirt instance bool [mscorlib]System.String::Contains(string)
						IL_015e: brtrue.s IL_0172

						IL_0160: ldloc.0
						IL_0161: callvirt instance string [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie::get_Value()
						IL_0166: ldstr ";"
						IL_016b: callvirt instance bool [mscorlib]System.String::Contains(string)
						IL_0170: br.s IL_0173

						IL_0172: ldc.i4.1

						IL_0173: nop
						IL_0174: stloc.s 10
						IL_0176: ldloc.s 10
						IL_0178: brtrue.s IL_0197

						IL_017a: ldarg.0
						IL_017b: ldfld class DouyinWindow/'<>c__DisplayClass23' DouyinWindow/'<SessionCookieAsync>d__25'::'CS$<>8__locals24'
						IL_0180: ldfld class [mscorlib]System.Collections.Generic.Dictionary`2<string, string> DouyinWindow/'<>c__DisplayClass23'::values
						IL_0185: ldloc.0
						IL_0186: callvirt instance string [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie::get_Name()
						IL_018b: ldloc.0
						IL_018c: callvirt instance string [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie::get_Value()
						IL_0191: callvirt instance void class [mscorlib]System.Collections.Generic.Dictionary`2<string, string>::set_Item(!0, !1)
						IL_0196: nop

						IL_0197: ldloca.s 9
						IL_0199: call instance bool valuetype [mscorlib]System.Collections.Generic.List`1/Enumerator<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>::MoveNext()
						IL_019e: stloc.s 10
						IL_01a0: ldloc.s 10
						IL_01a2: brtrue.s IL_012c
					// end loop

					IL_01a4: leave.s IL_01bf
				} // end .try
				finally
				{
					IL_01a6: ldloc.1
					IL_01a7: ldc.i4.0
					IL_01a8: ceq
					IL_01aa: stloc.s 10
					IL_01ac: ldloc.s 10
					IL_01ae: brtrue.s IL_01be

					IL_01b0: ldloca.s 9
					IL_01b2: constrained. valuetype [mscorlib]System.Collections.Generic.List`1/Enumerator<class [Microsoft.Web.WebView2.Core]Microsoft.Web.WebView2.Core.CoreWebView2Cookie>
					IL_01b8: callvirt instance void [mscorlib]System.IDisposable::Dispose()
					IL_01bd: nop

					IL_01be: endfinally
				} // end handler

				IL_01bf: nop
				IL_01c0: nop
				IL_01c1: ldarg.0
				IL_01c2: ldarg.0
				IL_01c3: ldfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>7__wrap29'
				IL_01c8: ldc.i4.1
				IL_01c9: add
				IL_01ca: stfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>7__wrap29'

				IL_01cf: ldarg.0
				IL_01d0: ldfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>7__wrap29'
				IL_01d5: ldarg.0
				IL_01d6: ldfld string[] DouyinWindow/'<SessionCookieAsync>d__25'::'<>7__wrap28'
				IL_01db: ldlen
				IL_01dc: conv.i4
				IL_01dd: clt
				IL_01df: stloc.s 10
				IL_01e1: ldloc.s 10
				IL_01e3: brtrue IL_0077

				IL_01e8: ldloc.1
				IL_01e9: ldc.i4.0
				IL_01ea: ceq
				IL_01ec: stloc.s 10
				IL_01ee: ldloc.s 10
				IL_01f0: brtrue.s IL_01f2

				IL_01f2: ldc.i4.3
				IL_01f3: newarr [mscorlib]System.String
				IL_01f8: stloc.s 5
				IL_01fa: ldloc.s 5
				IL_01fc: ldc.i4.0
				IL_01fd: ldstr "sessionid"
				IL_0202: stelem.ref
				IL_0203: ldloc.s 5
				IL_0205: ldc.i4.1
				IL_0206: ldstr "sessionid_ss"
				IL_020b: stelem.ref
				IL_020c: ldloc.s 5
				IL_020e: ldc.i4.2
				IL_020f: ldstr "sid_tt"
				IL_0214: stelem.ref
				IL_0215: ldloc.s 5
				IL_0217: ldarg.0
				IL_0218: ldfld class DouyinWindow/'<>c__DisplayClass23' DouyinWindow/'<SessionCookieAsync>d__25'::'CS$<>8__locals24'
				IL_021d: ldftn instance bool DouyinWindow/'<>c__DisplayClass23'::'<SessionCookieAsync>b__20'(string)
				IL_0223: newobj instance void class [mscorlib]System.Func`2<string, bool>::.ctor(object, native int)
				IL_0228: call bool [System.Core]System.Linq.Enumerable::Any<string>(class [mscorlib]System.Collections.Generic.IEnumerable`1<!!0>, class [mscorlib]System.Func`2<!!0, bool>)
				IL_022d: stloc.s 10
				IL_022f: ldloc.s 10
				IL_0231: brtrue.s IL_023e

				IL_0233: ldstr "请先在抖音官方页面完成登录，再点击授权。"
				IL_0238: newobj instance void [mscorlib]System.InvalidOperationException::.ctor(string)
				IL_023d: throw

				IL_023e: ldstr "; "
				IL_0243: ldarg.0
				IL_0244: ldfld class DouyinWindow/'<>c__DisplayClass23' DouyinWindow/'<SessionCookieAsync>d__25'::'CS$<>8__locals24'
				IL_0249: ldfld class [mscorlib]System.Collections.Generic.Dictionary`2<string, string> DouyinWindow/'<>c__DisplayClass23'::values
				IL_024e: ldsfld class [mscorlib]System.Func`2<valuetype [mscorlib]System.Collections.Generic.KeyValuePair`2<string, string>, string> DouyinWindow::'CS$<>9__CachedAnonymousMethodDelegate22'
				IL_0253: brtrue.s IL_0268

				IL_0255: ldnull
				IL_0256: ldftn string DouyinWindow::'<SessionCookieAsync>b__21'(valuetype [mscorlib]System.Collections.Generic.KeyValuePair`2<string, string>)
				IL_025c: newobj instance void class [mscorlib]System.Func`2<valuetype [mscorlib]System.Collections.Generic.KeyValuePair`2<string, string>, string>::.ctor(object, native int)
				IL_0261: stsfld class [mscorlib]System.Func`2<valuetype [mscorlib]System.Collections.Generic.KeyValuePair`2<string, string>, string> DouyinWindow::'CS$<>9__CachedAnonymousMethodDelegate22'
				IL_0266: br.s IL_0268

				IL_0268: ldsfld class [mscorlib]System.Func`2<valuetype [mscorlib]System.Collections.Generic.KeyValuePair`2<string, string>, string> DouyinWindow::'CS$<>9__CachedAnonymousMethodDelegate22'
				IL_026d: call class [mscorlib]System.Collections.Generic.IEnumerable`1<!!1> [System.Core]System.Linq.Enumerable::Select<valuetype [mscorlib]System.Collections.Generic.KeyValuePair`2<string, string>, string>(class [mscorlib]System.Collections.Generic.IEnumerable`1<!!0>, class [mscorlib]System.Func`2<!!0, !!1>)
				IL_0272: call string [mscorlib]System.String::Join(string, class [mscorlib]System.Collections.Generic.IEnumerable`1<string>)
				IL_0277: stloc.2
				IL_0278: leave.s IL_0292
			} // end .try
			catch [mscorlib]System.Exception
			{
				IL_027a: stloc.3
				IL_027b: ldarg.0
				IL_027c: ldc.i4.s -2
				IL_027e: stfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>1__state'
				IL_0283: ldarg.0
				IL_0284: ldflda valuetype [mscorlib]System.Runtime.CompilerServices.AsyncTaskMethodBuilder`1<string> DouyinWindow/'<SessionCookieAsync>d__25'::'<>t__builder'
				IL_0289: ldloc.3
				IL_028a: call instance void valuetype [mscorlib]System.Runtime.CompilerServices.AsyncTaskMethodBuilder`1<string>::SetException(class [mscorlib]System.Exception)
				IL_028f: nop
				IL_0290: leave.s IL_02a8
			} // end handler

			IL_0292: nop
			IL_0293: ldarg.0
			IL_0294: ldc.i4.s -2
			IL_0296: stfld int32 DouyinWindow/'<SessionCookieAsync>d__25'::'<>1__state'
			IL_029b: ldarg.0
			IL_029c: ldflda valuetype [mscorlib]System.Runtime.CompilerServices.AsyncTaskMethodBuilder`1<string> DouyinWindow/'<SessionCookieAsync>d__25'::'<>t__builder'
			IL_02a1: ldloc.2
			IL_02a2: call instance void valuetype [mscorlib]System.Runtime.CompilerServices.AsyncTaskMethodBuilder`1<string>::SetResult(!0)
			IL_02a7: nop

			IL_02a8: nop
			IL_02a9: ret
		} // end of method '<SessionCookieAsync>d__25'::MoveNext

		.method private final hidebysig newslot virtual 
			instance void SetStateMachine (
				class [mscorlib]System.Runtime.CompilerServices.IAsyncStateMachine param0
			) cil managed 
		{
			.custom instance void [mscorlib]System.Diagnostics.DebuggerHiddenAttribute::.ctor() = (
				01 00 00 00
			)
			.override method instance void [mscorlib]System.Runtime.CompilerServices.IAsyncStateMachine::SetStateMachine(class [mscorlib]System.Runtime.CompilerServices.IAsyncStateMachine)
			// Method begins at RVA 0x5ab4
			// Header size: 1
			// Code size: 13 (0xd)
			.maxstack 8

			IL_0000: ldarg.0
			IL_0001: ldflda valuetype [mscorlib]System.Runtime.CompilerServices.AsyncTaskMethodBuilder`1<string> DouyinWindow/'<SessionCookieAsync>d__25'::'<>t__builder'
			IL_0006: ldarg.1
			IL_0007: call instance void valuetype [mscorlib]System.Runtime.CompilerServices.AsyncTaskMethodBuilder`1<string>::SetStateMachine(class [mscorlib]System.Runtime.CompilerServices.IAsyncStateMachine)
			IL_000c: ret
		} // end of method '<SessionCookieAsync>d__25'::SetStateMachine

	} // end of class <SessionCookieAsync>d__25
