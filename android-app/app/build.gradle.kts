plugins {
    // AGP 9 applies Kotlin support itself; no separate kotlin-android plugin.
    alias(libs.plugins.android.application)
}

// Version comes from gradle.properties so a release only has to change one line. versionCode is
// derived rather than tracked by hand: Android needs it to increase on every install, and a
// number nobody has to remember to bump cannot be forgotten.
val appVersion: String = providers.gradleProperty("mpvbridge.version").get()
val appVersionCode: Int = appVersion.split(".").let { parts ->
    require(parts.size == 3) { "mpvbridge.version must be MAJOR.MINOR.PATCH, got '$appVersion'" }
    val (major, minor, patch) = parts.map(String::toInt)
    major * 10_000 + minor * 100 + patch
}

// Signing material is read from the environment, never from a file in the repo. Without it a
// release build is simply unsigned, which is what a fork or a CI run without secrets gets.
val keystorePath: String? = System.getenv("MPVBRIDGE_KEYSTORE")

android {
    namespace = "io.github.kpganon.mpvbridge"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.kpganon.mpvbridge"
        minSdk = 26
        targetSdk = 36
        versionCode = appVersionCode
        versionName = appVersion
    }

    signingConfigs {
        if (keystorePath != null) {
            create("release") {
                storeFile = file(keystorePath)
                storePassword = System.getenv("MPVBRIDGE_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("MPVBRIDGE_KEY_ALIAS") ?: "mpvbridge"
                keyPassword = System.getenv("MPVBRIDGE_KEY_PASSWORD")
            }
        }
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    buildTypes {
        release {
            // Media3 and the session service are reached reflectively in places; shrinking is not
            // worth debugging on a project this size for the few hundred KB it would save.
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release")
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
