"""All remote endpoints in one place (tests override these)."""

# Mojang / game files
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
MANIFEST_FALLBACK = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
JAVA_RUNTIMES = ("https://launchermeta.mojang.com/v1/products/java-runtime/"
                 "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json")
ASSETS_BASE = "https://resources.download.minecraft.net"
LIBRARIES_BASE = "https://libraries.minecraft.net/"

# authlib-injector (downloaded on demand; never bundled)
AUTHLIB_LATEST = [
    "https://authlib-injector.yushi.moe/artifact/latest.json",
    "https://bmclapi2.bangbang93.com/mirrors/authlib-injector/artifact/latest.json",
]

# Ely.by
ELY_AUTH = "https://authserver.ely.by"
ELY_INJECTOR = "https://authserver.ely.by/api/authlib-injector"
ELY_SKINS_PAGE = "https://ely.by/skins"

# Microsoft / Xbox / Minecraft services
MS_DEVICECODE = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
MS_TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_AUTH = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE = "https://api.minecraftservices.com/minecraft/profile"
MC_SKINS = "https://api.minecraftservices.com/minecraft/profile/skins"
MC_SKIN_ACTIVE = "https://api.minecraftservices.com/minecraft/profile/skins/active"
MC_LOOKUP_NAME = "https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}"
MOJANG_LOOKUP_NAME = "https://api.mojang.com/users/profiles/minecraft/{name}"
SESSION_PROFILE = "https://sessionserver.mojang.com/session/minecraft/profile/{uuid}"
