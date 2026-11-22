plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 版本号只在这里改，APK 文件名也跟着走：SameCast-<版本>-debug.apk
val samecastVersionName = "0.4.0"
val samecastVersionCode = 5

android {
    namespace = "com.samecast.receiver"
    compileSdk = 34
    // AGP 8.1.4 默认找 33.0.1，本机只装了 34.0.0，显式指定避免多下一套
    buildToolsVersion = "34.0.0"

    defaultConfig {
        applicationId = "com.samecast.receiver"
        // 微鲸 W40F 是 Android 5.x，minSdk 21 刚好覆盖
        minSdk = 21
        targetSdk = 34
        versionCode = samecastVersionCode
        versionName = samecastVersionName
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
}

// 产物叫 SameCast-0.4.0-debug.apk，而不是默认的 app-debug.apk
base {
    archivesName.set("SameCast-$samecastVersionName")
}

dependencies {
    implementation("androidx.media3:media3-exoplayer:1.3.1")
    implementation("androidx.media3:media3-exoplayer-hls:1.3.1")
    implementation("androidx.media3:media3-ui:1.3.1")
}
