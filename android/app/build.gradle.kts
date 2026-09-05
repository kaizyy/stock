plugins {
    id("com.android.application")
}

android {
    namespace = "nl.valerith.stockroom"
    compileSdk = 36

    defaultConfig {
        applicationId = "nl.valerith.stockroom"
        minSdk = 26
        targetSdk = 36
        versionCode = 3
        versionName = "2.0.0"

        val stockroomUrl = (project.findProperty("STOCKROOM_BASE_URL") as String?)
            ?.trim()
            ?.trimEnd('/')
            ?.takeIf { it.startsWith("https://") }
            ?: "https://stock.valerith.nl"
        buildConfigField("String", "STOCKROOM_BASE_URL", "\"$stockroomUrl\"")
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
