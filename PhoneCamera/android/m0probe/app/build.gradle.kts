plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.mouse.phoneprobe"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.mouse.phoneprobe"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    lint {
        abortOnError = false
    }
}

dependencies {
    // 扫码解码：zxing core（纯 Java，**不含** awt/javase 部分），jar 随仓库入库
    // ⇒ 仍然可以 `--offline` 构建，不引入任何网络/仓库依赖。
    //
    // 为什么用成熟库而不是自己写 QR 解码：自研要覆盖定位图形检测、透视采样、
    // 格式/版本信息、掩码、RS 纠错 —— 任何一处出错都表现为"扫不出来"，
    // 现场无法自查；而 zxing 是久经使用、行为可复现的实现。
    // 版本与哈希记在 docs/扫码配对.md 里（可复现性）。
    implementation(files("libs/core-3.5.3.jar"))
}
