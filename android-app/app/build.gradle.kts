plugins {
    // AGP 9 applies Kotlin support itself; no separate kotlin-android plugin.
    alias(libs.plugins.android.application)
}

android {
    namespace = "io.github.kpganon.termuxmpvcontrols"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.kpganon.termuxmpvcontrols"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.google.material)
    implementation(libs.androidx.activity)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.media3.session)
    implementation(libs.media3.common)
    implementation(libs.kotlinx.coroutines.android)
}
