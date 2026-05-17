from app.utils.config_util.config_loader import config

def main():
    print("config.get()=",config.get())
    # print(cfg["app"]["app_root"])

if __name__=="__main__":
    main()