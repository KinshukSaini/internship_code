Dreamina Seedance 2.0 series (hereinafter referred to as Seedance 2.0 series) models support multimodal input such as images, videos, audios and texts, with capabilities including video generation, video editing, and video extension. They can restore item details, timbres, effects, styles, camera movements and more with high accuracy, maintain consistent character features, and give users enterprise\-grade control. This topic introduces the exclusive capabilities of the Seedance 2.0 series models to help you get started quickly.
:::tip tip
Please purchase the [prepaid resource pack](https://www.byteplus.com/en/experience/modelark?launch=seedance-2-0) in advance, otherwise you cannot activate Seedance 2.0 series models.
:::
<span id="e000144b"></span>
# Getting started
This getting started tutorial is designed specifically for **users new to API**, to help you set up a Python development environment, create virtual environments, and install the ModelArk SDK with one click. With the provided out\-of\-the\-box Seedance 2.0 series code samples, you only need to replace the input assets to start your video creation.
<span id="480c43a8"></span>
## **1. Prerequisites**
Before you start, make sure you have completed the following preparations:

1. **Register an account**: Make sure you have a BytePlus account and are [signed in](https://console.byteplus.com/ark/region:ark+ap-southeast-1/overview).
2. **Get API Key**: Visit the [API keys](https://console.byteplus.com/ark/region:ark+ap-southeast-1/apiKey) page, click **Create API Key**, then copy and save your API Key. Make sure to keep your API Key safe and do not disclose it to others.
3. [Activate the models](https://console.byteplus.com/ark/region:ark+ap-southeast-1/openManagement): Please purchase the [prepaid resource packs](https://www.byteplus.com/en/experience/modelark?launch=seedance-2-0) in advance, otherwise you cannot activate Seedance 2.0 series models.
4. **Download and unzip the file**: Click to download the attachment below, and unzip it to your local directory (such as the desktop or "Downloads" folder).
   <Attachment link="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/f85a5be8202b45c1bb226669214af8c6~tplv-goo7wpa0wc-image.image" name="modelark_seedance2.0_quickstart_package.zip">modelark_seedance2.0_quickstart_package.zip</Attachment>


<span id="54b10004"></span>
## **2. Procedure**

```mixin-react
return (<Tabs>
<Tabs.TabPane title="Windows users" key="Uml7KwnMq4"><RenderMd content={`1. Go to the \`scripts/init_dev_env\` directory.
2. Double\\-click to run \`setup_windows.bat\`.
3. The script will automatically perform the following operations:
   * Download the uv tool.
   * Automatically download Python 3.12 (if it does not interfere with the Python installed in your system).
   * Create the \`.venv\` virtual environment.
   * Install the ModelArk SDK.
4. After completion, a \`run_demo.bat\` file will be generated in the project root directory.
5. Double\\-click \`run_demo.bat\` to run the Python SDK code sample (python/demo_standard.py).
`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="macOS users" key="zjEISKr5C9"><RenderMd content={`1. Open the terminal and go to the \`scripts/init_dev_env\` directory.
2. Run the set up script:
   \`\`\`Plain
   ./setup_mac.sh
   \`\`\`

3. The script will automatically configure all environments.
4. After completion, a \`run_demo.sh\` file will be generated in the project root directory.
5. Run \`./run_demo.sh\` to run the Python SDK code sample (python/demo_standard.py).
`}></RenderMd></Tabs.TabPane></Tabs>);
```

<span id="46454177"></span>
## **3. What the script** **does**
After running the script, you will see the following process:

1. **Verify the API Key**: The script will automatically detect whether the `ARK_API_KEY` environment variable is configured locally. If not, you will be prompted to enter it manually.
2. **Preview the assets**: The script will automatically pop up a locally generated HTML page in your default browser, displaying the text prompt for this task, the reference image to be replaced, and the reference video.
3. **Create the task and query for status**: The script initiates an asynchronous request to the ModelArk server. Since video generation takes some time, the console will print the task status (such as `running`) every 30 seconds.
4. **Get the results**: After the task is completed successfully, the console will output the URL of the generated video. You can copy the link to your browser to download or play it online.

<span id="370587e7"></span>
## **4. Next steps**
After you successfully run this sample, you can try to modify `python/demo_standard.py` to create your own video\-generation task:

1. Modify the text prompt.
   Find the `user_content` variable in the code and change it to any description you want.
2. Replace input assets (images, videos, audios).
   You can replace `reference_image_url`, `reference_video_url` and `reference_audio_url` with your own asset links. **Note**: Please make sure the URL is a publicly accessible link on the public network (it is recommended to store it in BytePlus TOS object storage service and configure it for public read access).
3. Continue to explore the following examples.

<span id="fd30cc1a"></span>
# Model capabilities
The Seedance 2.0 series models currently include Dreamina Seedance 2.0 (hereinafter referred to as Seedance 2.0) and Dreamina Seedance 2.0 Fast (hereinafter referred to as Seedance 2.0 Fast), which have the same model capabilities. For the highest generation quality, we recommend using Seedance 2.0. If you prioritize cost and generation speed over extreme quality, we recommend using Seedance 2.0 Fast.

<span aceTableMode="list" aceTableWidth="3,3,4,4"></span>
|Model Name ||[Seedance 2.0](https://console.byteplus.com/ark/region:ark+ap-southeast-1/model/detail?Id=dreamina-seedance-2-0) |[Seedance 2.0 Fast](https://console.byteplus.com/ark/region:ark+ap-southeast-1/model/detail?Id=dreamina-seedance-2-0-fast) |
|---|---|---|---|
|Model ID ||dreamina\-seedance\-2\-0\-260128 |dreamina\-seedance\-2\-0\-fast\-260128 |
|[Text to video](https://docs.byteplus.com/en/docs/ModelArk/2298881#4e74bcee) ||<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |
|[Image to video (first frame)](https://docs.byteplus.com/en/docs/ModelArk/2298881#979b2d28) ||<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |
|[Image to video (first and last frames)](https://docs.byteplus.com/en/docs/ModelArk/2298881#0d55ca07) ||<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |
|[Multimodal reference](https://docs.byteplus.com/en/docs/ModelArk/2291680#multimodal-reference) [New] |Image reference |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |
|^^|Video reference |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |
|^^|Combined reference|<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |\
| || | |\
| |* Image + audio| | |\
| |* Image + video| | |\
| |* Video + audio| | |\
| |* Image + video + audio | | |
|[Edit video](https://docs.byteplus.com/en/docs/ModelArk/2291680#edit-video) [New] ||<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |
|[Extend video](https://docs.byteplus.com/en/docs/ModelArk/2291680#extend-video) [New] ||<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |
|[Generate audio video](https://docs.byteplus.com/en/docs/ModelArk/2298881#979b2d28)||<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |\
|> "generate_audio": "true" | | | |
|[Draft mode](https://docs.byteplus.com/en/docs/ModelArk/2298881#5acd28c8) ||<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/f359753773c94d97885008ca1223c9bc~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/f359753773c94d97885008ca1223c9bc~tplv-goo7wpa0wc-image.image =20x) </span> |
|[Return the last frame of the generated video](https://docs.byteplus.com/en/docs/ModelArk/2298881#141cf7fa)||<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/ee51ce32c1914aed81ff95080bb7db1d~tplv-goo7wpa0wc-image.image =20x) </span> |\
|> "return_last_frame": "true" | | | |
|[Output video specifications](https://docs.byteplus.com/en/docs/ModelArk/2298881#9fe4cce0) |Resolution|480p, 720p, 1080p |480p, 720p |\
| |> "resolution": "720p" | | |
|^^|Aspect ratio|21:9, 16:9, 4:3, 1:1, 3:4, 9:16 |21:9, 16:9, 4:3, 1:1, 3:4, 9:16 |\
| |> "ratio":"16:9" | | |
|^^|Duration|4–15 seconds |4–15 seconds |\
| |> "duration": 5 | | |
|^^|Video format |mp4 |mp4 |
|[Offline inference](https://docs.byteplus.com/en/docs/ModelArk/2298881#a0badaae)||<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/f359753773c94d97885008ca1223c9bc~tplv-goo7wpa0wc-image.image =20x) </span> |<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/f359753773c94d97885008ca1223c9bc~tplv-goo7wpa0wc-image.image =20x) </span> |\
|> "service_tier": "flex" | | | |
|Online inference rate limits |Max. RPM |Enterprise users: 600 Individual users: 180 |Enterprise users: 600 Individual users: 180 |
|^^|Max. concurrency |Enterprise users: 10|Enterprise users: 10|\
| | |Individual users: 3 |Individual users: 3 |
|Offline inference rate limits |Max. TPD |\- |\- |

<span id="dcb767c3"></span>
# Basic usage
<span id="50e1b4ea"></span>
## Multimodal reference
Input text, reference images, videos (with or without audio tracks) and audios to generate a new video. It can inherit core information including character image, visual style and screen composition from reference images, subject, camera movement, action performance and overall style from reference videos, as well as timbre, music melody and dialogue content from reference audios.
The following are some demos (visit the [model card](https://console.byteplus.com/ark/region:ark+ap-southeast-1/model/detail?Id=dreamina-seedance-2-0) to view more samples):

<span aceTableMode="list" aceTableWidth="3,2,2"></span>
|Input: text |Input: image, video, audio |Output |
|---|---|---|
|Use the first\-person POV framing from [Video 1] throughout, and use [Audio 1] as the background music throughout. First\-person POV fruit tea promotional ad, seedance limited\-edition apple fruit tea; opening frame is [Image 1], your hand picks a dew\-covered Aksu red apple, a light, crisp apple tapping sound; 2–4 seconds: fast cuts, your hand drops apple chunks into a shaker, adds ice and tea base, shakes forcefully, ice clinking and shaking sounds sync with upbeat rhythmic beats, background audio: {Fresh\-cut, shaken fresh}; 4–6 seconds: first\-person close\-up of the finished drink, layered fruit tea is poured into a clear cup, your hand gently squeezes milk foam to spread across the top, a pink brand sticker is applied to the cup, the camera moves closer to show the layered textures of the foam and fruit tea; 6–8 seconds: first\-person hand\-held toast shot, you raise the fruit tea from [Image 2] toward the camera (simulating handing it to the viewer), the cup label is clearly visible, background audio {Take a sip of fresh refreshment}, the final frame freezes on Image 2. All background voice audio uses a female voice. |<video src="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/0ba05cd435f543c5bc65c378d94d094a~tplv-goo7wpa0wc-image.image" controls></video>|<video src="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/74d43d21b6cf40248c8060bc8181c318~tplv-goo7wpa0wc-image.image" controls></video>|\
| | | |


```mixin-react
return (<Tabs>
<Tabs.TabPane title="Python" key="MlRa9eaFt1"><RenderMd content={`\`\`\`Python
import os
import time
# Install SDK:pip install byteplus-python-sdk-v2
from byteplussdkarkruntime import Ark

client = Ark(
    #The base URL for model invocation
    base_url='https://ark.ap-southeast.bytepluses.com/api/v3',
    # Get API Key: https://console.byteplus.com/ark/region:ark+ap-southeast-1/apikey
    api_key=os.environ.get("ARK_API_KEY"),
)

if __name__ == "__main__":
    print("----- create request -----")
    create_result = client.content_generation.tasks.create(
        model="dreamina-seedance-2-0-260128", #Replace with Model ID
        content=[
            \{
                "type": "text",
                "text": "Use the first-person POV framing from Video 1 throughout, and use Audio 1 as the background music throughout. First-person POV fruit tea promotional ad, seedance limited-edition apple fruit tea; opening frame is Image 1, your hand picks a dew-covered Aksu red apple, a light, crisp apple tapping sound; 2–4 seconds: fast cuts, your hand drops apple chunks into a shaker, adds ice and tea base, shakes forcefully, ice clinking and shaking sounds sync with upbeat rhythmic beats, background audio: \{Fresh-cut, shaken fresh\}; 4–6 seconds: first-person close-up of the finished drink, layered fruit tea is poured into a clear cup, your hand gently squeezes milk foam to spread across the top, a pink brand sticker is applied to the cup, the camera moves closer to show the layered textures of the foam and fruit tea; 6–8 seconds: first-person hand-held toast shot, you raise the fruit tea from Image 2 toward the camera (simulating handing it to the viewer), the cup label is clearly visible, background audio \{Take a sip of fresh refreshment\}, the final frame freezes on Image 2. All background voice audio uses a female voice.",
            \},
            \{
                "type": "image_url",
                "image_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_tea_pic1.jpg"
                \},
                "role": "reference_image",
            \},
            \{
                "type": "image_url",
                "image_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_tea_pic2.jpg"
                \},
                "role": "reference_image",
            \},
            \{
                "type": "video_url",
                "video_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_tea_video1.mp4"
                \},
                "role": "reference_video",
            \},
            \{
                "type": "audio_url",
                "audio_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_audio/r2v_tea_audio1.mp3"
                \},
                "role": "reference_audio",
            \},
        ],
        generate_audio=True,
        ratio="16:9",
        duration=11,
        watermark=True,
    )
    print(create_result)


    # Polling query section
    print("----- polling task status -----")
    task_id = create_result.id
    while True:
        get_result = client.content_generation.tasks.get(task_id=task_id)
        status = get_result.status
        if status == "succeeded":
            print("----- task succeeded -----")
            print(get_result)
            break
        elif status == "failed":
            print("----- task failed -----")
            print(f"Error: \{get_result.error\}")
            break
        else:
            print(f"Current status: \{status\}, Retrying after 30 seconds...")
            time.sleep(30)
\`\`\`

`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="Java" key="v24eG5g7i9"><RenderMd content={`\`\`\`Java
package com.ark.sample;

import com.byteplus.ark.runtime.model.content.generation.*;
import com.byteplus.ark.runtime.model.content.generation.CreateContentGenerationTaskRequest.Content;
import com.byteplus.ark.runtime.service.ArkService;
import okhttp3.ConnectionPool;
import okhttp3.Dispatcher;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

public class ContentGenerationTaskExample \{

    // Client initialization
    static String apiKey = System.getenv("ARK_API_KEY");
    static ConnectionPool connectionPool = new ConnectionPool(5, 1, TimeUnit.SECONDS);
    static Dispatcher dispatcher = new Dispatcher();
    static ArkService service = ArkService.builder()
           .baseUrl("https://ark.ap-southeast.bytepluses.com/api/v3") //The base URL for model invocation
           .dispatcher(dispatcher)
           .connectionPool(connectionPool)
           .apiKey(apiKey)
           .build();

    public static void main(String[] args) \{

        // Model ID
        final String modelId = "dreamina-seedance-2-0-260128";
        // Text prompt
        final String prompt = "Use the first-person POV framing from [Video 1] throughout, and use [Audio 1] as the background music throughout. First-person POV fruit tea promotional ad, seedance limited-edition apple fruit tea; " +
                "opening frame is [Image 1], your hand picks a dew-covered Aksu red apple, a light, crisp apple tapping sound;" +
                "2–4 seconds: fast cuts, your hand drops apple chunks into a shaker, adds ice and tea base, shakes forcefully, ice clinking and shaking sounds sync with upbeat rhythmic beats, background audio: \{Fresh-cut, shaken fresh\}; " +
                "4–6 seconds: first-person close-up of the finished drink, layered fruit tea is poured into a clear cup, your hand gently squeezes milk foam to spread across the top, a pink brand sticker is applied to the cup, the camera moves closer to show the layered textures of the foam and fruit tea;" +
                "6–8 seconds: first-person hand-held toast shot, you raise the fruit tea from [Image 2] toward the camera (simulating handing it to the viewer), the cup label is clearly visible, background audio \{Take a sip of fresh refreshment\}, the final frame freezes on Image 2." +
                "All background voice audio uses a female voice.";

        // Example resource URLs
        final String refImage1 = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_tea_pic1.jpg";
        final String refImage2 = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_tea_pic2.jpg";
        final String refVideo = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_tea_video1.mp4";
        final String refAudio = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_audio/r2v_tea_audio1.mp3";

        // Output video parameters
        final boolean generateAudio = true;
        final String videoRatio = "16:9";
        final long videoDuration = 11L;
        final boolean showWatermark = true;

        System.out.println("----- create request -----");
        // Build request content
        List<Content> contents = new ArrayList<>();

        // 1. Text prompt
        contents.add(Content.builder()
                .type("text")
                .text(prompt)
                .build());

        // 2. Reference image 1
        contents.add(Content.builder()
                .type("image_url")
                .imageUrl(CreateContentGenerationTaskRequest.ImageUrl.builder()
                        .url(refImage1)
                        .build())
                .role("reference_image")
                .build());

        // 3. Reference image 2
        contents.add(Content.builder()
                .type("image_url")
                .imageUrl(CreateContentGenerationTaskRequest.ImageUrl.builder()
                        .url(refImage2)
                        .build())
                .role("reference_image")
                .build());

        // 4. Reference video
        contents.add(Content.builder()
                .type("video_url")
                .videoUrl(CreateContentGenerationTaskRequest.VideoUrl.builder()
                        .url(refVideo)
                        .build())
                .role("reference_video")
                .build());

        // 5. Reference audio
        contents.add(Content.builder()
                .type("audio_url")
                .audioUrl(CreateContentGenerationTaskRequest.AudioUrl.builder()
                        .url(refAudio)
                        .build())
                .role("reference_audio")
                .build());

        // Create video generation task
        CreateContentGenerationTaskRequest createRequest = CreateContentGenerationTaskRequest.builder()
                .generateAudio(generateAudio)
                .model(modelId)
                .content(contents)
                .ratio(videoRatio)
                .duration(videoDuration)
                .watermark(showWatermark)
                .build();

        CreateContentGenerationTaskResult createResult = service.createContentGenerationTask(createRequest);
        System.out.println("Task Created: " + createResult);

        // Get task details and poll status
        String taskId = createResult.getId();
        pollTaskStatus(taskId);
    \}

    /**
     * Poll task status
     *@param taskId Task ID
     */

    private static void pollTaskStatus(String taskId) \{
        GetContentGenerationTaskRequest getRequest = GetContentGenerationTaskRequest.builder()
                .taskId(taskId)
                .build();

        System.out.println("----- polling task status -----");
        try \{
            while (true) \{
                GetContentGenerationTaskResponse getResponse = service.getContentGenerationTask(getRequest);
                String status = getResponse.getStatus();

                if ("succeeded".equalsIgnoreCase(status)) \{
                    System.out.println("----- task succeeded -----");
                    System.out.println(getResponse);
                    break;
                \} else if ("failed".equalsIgnoreCase(status)) \{
                    System.out.println("----- task failed -----");
                    if (getResponse.getError() != null) \{
                        System.out.println("Error: " + getResponse.getError().getMessage());
                    \}
                    break;
                \} else \{
                    System.out.printf("Current status: %s, Retrying in 10 seconds...%n", status);
                    TimeUnit.SECONDS.sleep(10);
                \}
            \}
        \} catch (InterruptedException ie) \{
            Thread.currentThread().interrupt();
            System.err.println("Polling interrupted");
        \} catch (Exception e) \{
            System.err.println("Error occurred: " + e.getMessage());
        \} finally \{
            service.shutdownExecutor();
        \}
    \}
\}
\`\`\`

`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="Go" key="UN3vTQu7ed"><RenderMd content={`\`\`\`Go
package main

import (
    "context"
    "fmt"
    "os"
    "time"

    "github.com/byteplus-sdk/byteplus-go-sdk-v2/service/arkruntime"
    "github.com/byteplus-sdk/byteplus-go-sdk-v2/service/arkruntime/model"
    "github.com/byteplus-sdk/byteplus-go-sdk-v2/byteplus"
)

func main() \{
    // Initialize Ark client
    client := arkruntime.NewClientWithApiKey(
        os.Getenv("ARK_API_KEY"),
        //The base URL for model invocation
        arkruntime.WithBaseUrl("https://ark.ap-southeast.bytepluses.com/api/v3"),
    )
    ctx := context.Background()

    // Model ID
    modelID := "dreamina-seedance-2-0-260128"
    // Text prompt
    prompt := "Use the first-person POV framing from [Video 1] throughout, and use [Audio 1] as the background music throughout. First-person POV fruit tea promotional ad, seedance limited-edition apple fruit tea; " +
                "opening frame is [Image 1], your hand picks a dew-covered Aksu red apple, a light, crisp apple tapping sound;" +
                "2–4 seconds: fast cuts, your hand drops apple chunks into a shaker, adds ice and tea base, shakes forcefully, ice clinking and shaking sounds sync with upbeat rhythmic beats, background audio: \{Fresh-cut, shaken fresh\}; " +
                "4–6 seconds: first-person close-up of the finished drink, layered fruit tea is poured into a clear cup, your hand gently squeezes milk foam to spread across the top, a pink brand sticker is applied to the cup, the camera moves closer to show the layered textures of the foam and fruit tea;" +
                "6–8 seconds: first-person hand-held toast shot, you raise the fruit tea from [Image 2] toward the camera (simulating handing it to the viewer), the cup label is clearly visible, background audio \{Take a sip of fresh refreshment\}, the final frame freezes on Image 2." +
                "All background voice audio uses a female voice."

    // Example resource URLs
    refImage1 := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_tea_pic1.jpg"
    refImage2 := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_tea_pic2.jpg"
    refVideo := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_tea_video1.mp4"
    refAudio := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_audio/r2v_tea_audio1.mp3"

    // Output video parameters
    generateAudio := true
    videoRatio := "16:9"
    videoDuration := int64(11)
    showWatermark := true

    // 1. Create video generation task
    fmt.Println("----- create request -----")
    createReq := model.CreateContentGenerationTaskRequest\{
        Model:         modelID,
        GenerateAudio: byteplus.Bool(generateAudio),
        Ratio:         byteplus.String(videoRatio),
        Duration:      byteplus.Int64(videoDuration),
        Watermark:     byteplus.Bool(showWatermark),
        Content: []*model.CreateContentGenerationContentItem\{
            \{
                Type: model.ContentGenerationContentItemTypeText,
                Text: byteplus.String(prompt),
            \},
            \{
                Type: model.ContentGenerationContentItemType("image_url"),
                ImageURL: &model.ImageURL\{
                    URL: refImage1,
                \},
                Role: byteplus.String("reference_image"),
            \},
            \{
                Type: model.ContentGenerationContentItemType("image_url"),
                ImageURL: &model.ImageURL\{
                    URL: refImage2,
                \},
                Role: byteplus.String("reference_image"),
            \},
            \{
                Type: model.ContentGenerationContentItemType("video_url"),
                VideoURL: &model.VideoUrl\{
                    Url: refVideo,
                \},
                Role: byteplus.String("reference_video"),
            \},
            \{
                Type: model.ContentGenerationContentItemType("audio_url"),
                AudioURL: &model.AudioUrl\{
                    Url: refAudio,
                \},
                Role: byteplus.String("reference_audio"),
            \},
        \},
    \}

    createResp, err := client.CreateContentGenerationTask(ctx, createReq)
    if err != nil \{
        fmt.Printf("create content generation error: %v\\\\n", err)
        return
    \}

    taskID := createResp.ID
    fmt.Printf("Task Created with ID: %s\\\\n", taskID)

    // 2. Poll task status
    pollTaskStatus(ctx, client, taskID)
\}

// poll task status
func pollTaskStatus(ctx context.Context, client *arkruntime.Client, taskID string) \{
    fmt.Println("----- polling task status -----")
    for \{
        getReq := model.GetContentGenerationTaskRequest\{ID: taskID\}
        getResp, err := client.GetContentGenerationTask(ctx, getReq)
        if err != nil \{
            fmt.Printf("get content generation task error: %v\\\\n", err)
            return
        \}

        status := getResp.Status
        if status == "succeeded" \{
            fmt.Println("----- task succeeded -----")
            fmt.Printf("Task ID: %s \\\\n", getResp.ID)
            fmt.Printf("Model: %s \\\\n", getResp.Model)
            fmt.Printf("Video URL: %s \\\\n", getResp.Content.VideoURL)
            fmt.Printf("Completion Tokens: %d \\\\n", getResp.Usage.CompletionTokens)
            fmt.Printf("Created At: %d, Updated At: %d\\\\n", getResp.CreatedAt, getResp.UpdatedAt)
            return
        \} else if status == "failed" \{
            fmt.Println("----- task failed -----")
            if getResp.Error != nil \{
                fmt.Printf("Error Code: %s, Message: %s\\\\n", getResp.Error.Code, getResp.Error.Message)
            \}
            return
        \} else \{
            fmt.Printf("Current status: %s, Retrying in 10 seconds... \\\\n", status)
            time.Sleep(10 * time.Second)
        \}
    \}
\}
\`\`\`

`}></RenderMd></Tabs.TabPane></Tabs>);
```

:::tip tip

* You can combine the following modal content as needed. Note that "text + audio" and "audio\-only" inputs are not supported.
   * Text
   * Images: 0–9 images
   * Videos: 0–3 videos
   * Audio: 0–3 audios
* **Advanced usage**: For multimodal video generation, you can specify reference images as the first/last frame via prompts to indirectly achieve the effect of "first and last frames + multimodal reference". If you need to strictly ensure that the first and last frames are consistent with the specified images, please **always use image\-to\-video (first and last frame)**  feature (configure the `role` parameter to `first_frame` / `last_frame`).
* See [Multimodal input](/docs/ModelArk/2298881#63a97f09) for input requirements for each modal information.

:::
<span id="75a28782"></span>
## Edit video
You can provide the video to be edited, reference images or audio, and use prompts together to complete various video editing tasks, such as replacing the video subject, adding, deleting and modifying objects in the video, redrawing/repairing partial frames, etc.
The following are some demos (visit the [model card](https://console.byteplus.com/ark/region:ark+ap-southeast-1/model/detail?Id=dreamina-seedance-2-0) to view more samples):

<span aceTableMode="list" aceTableWidth="3,2,2"></span>
|Input: text |Input: video & image |Output |
|---|---|---|
|Replace the cat in [Video 1] with the lion from [Image 1]. The lion lies on its side across the girl’s legs, gently interacting with her in a warm and tender way. |<video src="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/b172c1b5adf04d6d96f86d19a06bb00a~tplv-goo7wpa0wc-image.image" controls></video>|<video src="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/b0230aa7a0ba4068a03576b6a251f99f~tplv-goo7wpa0wc-image.image" controls></video>|\
| | | |


```mixin-react
return (<Tabs>
<Tabs.TabPane title="Python" key="S6hY8Xz7v0"><RenderMd content={`\`\`\`Python
import os
import time
# Install SDK:pip install byteplus-python-sdk-v2
from byteplussdkarkruntime import Ark

client = Ark(
    #The base URL for model invocation
    base_url='https://ark.ap-southeast.bytepluses.com/api/v3',
    # Get API Key: https://console.byteplus.com/ark/region:ark+ap-southeast-1/apikey
    api_key=os.environ.get("ARK_API_KEY"),
)

if __name__ == "__main__":
    print("----- create request -----")
    create_result = client.content_generation.tasks.create(
        model="dreamina-seedance-2-0-260128", #Replace with Model ID
        content=[
            \{
                "type": "text",
                "text": "Replace the cat in [Video1] with the lion from [Image1]. The lion lies on its side across the girl’s legs, gently interacting with her in a warm and tender way.",
            \},
            \{
                "type": "image_url",
                "image_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_edit_pic1.jpg"
                \},
                "role": "reference_image",
            \},
            \{
                "type": "video_url",
                "video_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_edit_video1.mp4"
                \},
                "role": "reference_video",
            \},
        ],
        generate_audio=True,
        ratio="16:9",
        duration=5,
        duration=12,
        watermark=True,
    )
    print(create_result)


    # Polling query section
    print("----- polling task status -----")
    task_id = create_result.id
    while True:
        get_result = client.content_generation.tasks.get(task_id=task_id)
        status = get_result.status
        if status == "succeeded":
            print("----- task succeeded -----")
            print(get_result)
            break
        elif status == "failed":
            print("----- task failed -----")
            print(f"Error: \{get_result.error\}")
            break
        else:
            print(f"Current status: \{status\}, Retrying after 30 seconds...")
            time.sleep(30)
\`\`\`

`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="Java" key="BQ6nxjRJLy"><RenderMd content={`\`\`\`Java
package com.ark.sample;

import com.byteplus.ark.runtime.model.content.generation.*;
import com.byteplus.ark.runtime.model.content.generation.CreateContentGenerationTaskRequest.Content;
import com.byteplus.ark.runtime.service.ArkService;
import okhttp3.ConnectionPool;
import okhttp3.Dispatcher;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

public class ContentGenerationTaskExample \{

    // Client initialization
    static String apiKey = System.getenv("ARK_API_KEY");
    static ConnectionPool connectionPool = new ConnectionPool(5, 1, TimeUnit.SECONDS);
    static Dispatcher dispatcher = new Dispatcher();
    static ArkService service = ArkService.builder()
           .baseUrl("https://ark.ap-southeast.bytepluses.com/api/v3") //The base URL for model invocation
           .dispatcher(dispatcher)
           .connectionPool(connectionPool)
           .apiKey(apiKey)
           .build();

    public static void main(String[] args) \{

        // Model ID
        final String modelId = "dreamina-seedance-2-0-260128";
        // Text prompt
        final String prompt = "Replace the cat in [Video 1] with the lion from [Image 1]. The lion lies on its side across the girl’s legs, gently interacting with her in a warm and tender way.";

        // Example resource URLs
        final String refImage1 = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_edit_pic1.jpg";
        final String refVideo = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_edit_video1.mp4";

        // Output video parameters
        final boolean generateAudio = true;
        final String videoRatio = "16:9";
        final long videoDuration = 5L;
        final boolean showWatermark = true;

        System.out.println("----- create request -----");
        // Build request content
        List<Content> contents = new ArrayList<>();

        // 1. Text prompt
        contents.add(Content.builder()
                .type("text")
                .text(prompt)
                .build());

        // 2. Reference image 1
        contents.add(Content.builder()
                .type("image_url")
                .imageUrl(CreateContentGenerationTaskRequest.ImageUrl.builder()
                        .url(refImage1)
                        .build())
                .role("reference_image")
                .build());

        // 3. Reference video
        contents.add(Content.builder()
                .type("video_url")
                .videoUrl(CreateContentGenerationTaskRequest.VideoUrl.builder()
                        .url(refVideo)
                        .build())
                .role("reference_video")
                .build());

        // Create video generation task
        CreateContentGenerationTaskRequest createRequest = CreateContentGenerationTaskRequest.builder()
                .generateAudio(generateAudio)
                .model(modelId)
                .content(contents)
                .ratio(videoRatio)
                .duration(videoDuration)
                .watermark(showWatermark)
                .build();

        CreateContentGenerationTaskResult createResult = service.createContentGenerationTask(createRequest);
        System.out.println("Task Created: " + createResult);

        // Get task details and poll status
        String taskId = createResult.getId();
        pollTaskStatus(taskId);
    \}

    /**
     * Poll task status
     *@param taskId Task ID
     */

    private static void pollTaskStatus(String taskId) \{
        GetContentGenerationTaskRequest getRequest = GetContentGenerationTaskRequest.builder()
                .taskId(taskId)
                .build();

        System.out.println("----- polling task status -----");
        try \{
            while (true) \{
                GetContentGenerationTaskResponse getResponse = service.getContentGenerationTask(getRequest);
                String status = getResponse.getStatus();

                if ("succeeded".equalsIgnoreCase(status)) \{
                    System.out.println("----- task succeeded -----");
                    System.out.println(getResponse);
                    break;
                \} else if ("failed".equalsIgnoreCase(status)) \{
                    System.out.println("----- task failed -----");
                    if (getResponse.getError() != null) \{
                        System.out.println("Error: " + getResponse.getError().getMessage());
                    \}
                    break;
                \} else \{
                    System.out.printf("Current status: %s, Retrying in 10 seconds...%n", status);
                    TimeUnit.SECONDS.sleep(10);
                \}
            \}
        \} catch (InterruptedException ie) \{
            Thread.currentThread().interrupt();
            System.err.println("Polling interrupted");
        \} catch (Exception e) \{
            System.err.println("Error occurred: " + e.getMessage());
        \} finally \{
            service.shutdownExecutor();
        \}
    \}
\}
\`\`\`

`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="Go" key="bkSwVuSRQo"><RenderMd content={`\`\`\`Go
package main

import (
    "context"
    "fmt"
    "os"
    "time"

    "github.com/byteplus-sdk/byteplus-go-sdk-v2/service/arkruntime"
    "github.com/byteplus-sdk/byteplus-go-sdk-v2/service/arkruntime/model"
    "github.com/byteplus-sdk/byteplus-go-sdk-v2/byteplus"
)

func main() \{
    // Initialize Ark client
    client := arkruntime.NewClientWithApiKey(
        os.Getenv("ARK_API_KEY"),
        //The base URL for model invocation
        arkruntime.WithBaseUrl("https://ark.ap-southeast.bytepluses.com/api/v3"),
    )
    ctx := context.Background()

    // Model ID
    modelID := "dreamina-seedance-2-0-260128"
    // Text prompt
    prompt := "Replace the cat in [Video 1] with the lion from [Image 1]. The lion lies on its side across the girl’s legs, gently interacting with her in a warm and tender way."

    // Example resource URLs
    refImage1 := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_edit_pic1.jpg"
    refVideo1 := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_edit_video1.mp4"

    // Output video parameters
    generateAudio := true
    videoRatio := "16:9"
    videoDuration := int64(5)
    showWatermark := true

    // 1. Create video generation task
    fmt.Println("----- create request -----")
    createReq := model.CreateContentGenerationTaskRequest\{
        Model:         modelID,
        GenerateAudio: byteplus.Bool(generateAudio),
        Ratio:         byteplus.String(videoRatio),
        Duration:      byteplus.Int64(videoDuration),
        Watermark:     byteplus.Bool(showWatermark),
        Content: []*model.CreateContentGenerationContentItem\{
            \{
                Type: model.ContentGenerationContentItemTypeText,
                Text: byteplus.String(prompt),
            \},
            \{
                Type: model.ContentGenerationContentItemType("image_url"),
                ImageURL: &model.ImageURL\{
                    URL: refImage1,
                \},
                Role: byteplus.String("reference_image"),
            \},
            \{
                Type: model.ContentGenerationContentItemType("video_url"),
                VideoURL: &model.VideoUrl\{
                    Url: refVideo1,
                \},
                Role: byteplus.String("reference_video"),
            \},
        \},
    \}

    createResp, err := client.CreateContentGenerationTask(ctx, createReq)
    if err != nil \{
        fmt.Printf("create content generation error: %v\\\\n", err)
        return
    \}

    taskID := createResp.ID
    fmt.Printf("Task Created with ID: %s\\\\n", taskID)

    // 2. Poll task status
    pollTaskStatus(ctx, client, taskID)
\}

// poll task status
func pollTaskStatus(ctx context.Context, client *arkruntime.Client, taskID string) \{
    fmt.Println("----- polling task status -----")
    for \{
        getReq := model.GetContentGenerationTaskRequest\{ID: taskID\}
        getResp, err := client.GetContentGenerationTask(ctx, getReq)
        if err != nil \{
            fmt.Printf("get content generation task error: %v\\\\n", err)
            return
        \}

        status := getResp.Status
        if status == "succeeded" \{
            fmt.Println("----- task succeeded -----")
            fmt.Printf("Task ID: %s \\\\n", getResp.ID)
            fmt.Printf("Model: %s \\\\n", getResp.Model)
            fmt.Printf("Video URL: %s \\\\n", getResp.Content.VideoURL)
            fmt.Printf("Completion Tokens: %d \\\\n", getResp.Usage.CompletionTokens)
            fmt.Printf("Created At: %d, Updated At: %d\\\\n", getResp.CreatedAt, getResp.UpdatedAt)
            return
        \} else if status == "failed" \{
            fmt.Println("----- task failed -----")
            if getResp.Error != nil \{
                fmt.Printf("Error Code: %s, Message: %s\\\\n", getResp.Error.Code, getResp.Error.Message)
            \}
            return
        \} else \{
            fmt.Printf("Current status: %s, Retrying in 10 seconds... \\\\n", status)
            time.Sleep(10 * time.Second)
        \}
    \}
\}
\`\`\`

`}></RenderMd></Tabs.TabPane></Tabs>);
```

<span id="46d77653"></span>
## Extend video
Based on the original video, you can extend the video forward or backward, or stitch multiple video clips (up to 3 clips) into a coherent video.
The following are some demos (visit the [model card](https://console.byteplus.com/ark/region:ark+ap-southeast-1/model/detail?Id=dreamina-seedance-2-0) to view more samples):

<span aceTableMode="list" aceTableWidth="3,2,2"></span>
|Input: text |Input: video to be extended |Output |
|---|---|---|
|The arched window in [video 1] opens, and the camera moves into the interior of the art museum, transitioning into [video 2]. After that, the camera enters the painting itself, transitioning into [video 3]. |<video src="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/54519ff7266d4f1caa12b8cc95e2dd1d~tplv-goo7wpa0wc-image.image" controls></video>|<video src="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/849b3f86f609495ca09d559aa14c79ed~tplv-goo7wpa0wc-image.image" controls></video>|\
| | | |


```mixin-react
return (<Tabs>
<Tabs.TabPane title="Python" key="RAUpqqmAG6"><RenderMd content={`\`\`\`Python
import os
import time
# Install SDK:pip install byteplus-python-sdk-v2
from byteplussdkarkruntime import Ark

client = Ark(
    #The base URL for model invocation
    base_url='https://ark.ap-southeast.bytepluses.com/api/v3',
    # Get API Key: https://console.byteplus.com/ark/region:ark+ap-southeast-1/apikey
    api_key=os.environ.get("ARK_API_KEY"),
)

if __name__ == "__main__":
    print("----- create request -----")
    create_result = client.content_generation.tasks.create(
        model="dreamina-seedance-2-0-260128", #Replace with Model ID
        content=[
            \{
                "type": "text",
                "text": "The arched window in [video 1] opens, and the camera moves into the interior of the art museum, transitioning into [video 2]. After that, the camera enters the painting itself, transitioning into [video 3].",

            \},
            \{
                "type": "video_url",
                "video_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_extend_video1.mp4"
                \},
                "role": "reference_video",
            \},
            \{
                "type": "video_url",
                "video_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_extend_video2.mp4"
                \},
                "role": "reference_video",
            \},
            \{
                "type": "video_url",
                "video_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_extend_video3.mp4"
                \},
                "role": "reference_video",
            \},
        ],
        generate_audio=True,
        ratio="16:9",
        duration=8,
        watermark=True,
    )
    print(create_result)


    # Polling query section
    print("----- polling task status -----")
    task_id = create_result.id
    while True:
        get_result = client.content_generation.tasks.get(task_id=task_id)
        status = get_result.status
        if status == "succeeded":
            print("----- task succeeded -----")
            print(get_result)
            break
        elif status == "failed":
            print("----- task failed -----")
            print(f"Error: \{get_result.error\}")
            break
        else:
            print(f"Current status: \{status\}, Retrying after 30 seconds...")
            time.sleep(30)
\`\`\`

`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="Java" key="teVGcVmEUD"><RenderMd content={`\`\`\`Java
package com.ark.sample;

import com.byteplus.ark.runtime.model.content.generation.*;
import com.byteplus.ark.runtime.model.content.generation.CreateContentGenerationTaskRequest.Content;
import com.byteplus.ark.runtime.service.ArkService;
import okhttp3.ConnectionPool;
import okhttp3.Dispatcher;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

public class ContentGenerationTaskExample \{

    // Client initialization
    static String apiKey = System.getenv("ARK_API_KEY");
    static ConnectionPool connectionPool = new ConnectionPool(5, 1, TimeUnit.SECONDS);
    static Dispatcher dispatcher = new Dispatcher();
    static ArkService service = ArkService.builder()
           .baseUrl("https://ark.ap-southeast.bytepluses.com/api/v3") //The base URL for model invocation
           .dispatcher(dispatcher)
           .connectionPool(connectionPool)
           .apiKey(apiKey)
           .build();

    public static void main(String[] args) \{

        // Model ID
        final String modelId = "dreamina-seedance-2-0-260128";
        // Text prompt
        final String prompt = "The arched window in [video 1] opens, and the camera moves into the interior of the art museum, transitioning into [video 2]. After that, the camera enters the painting itself, transitioning into [video 3].";

        // Example resource URLs
        final String refVideo1 = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_extend_video1.mp4";
        final String refVideo2 = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_extend_video2.mp4";
        final String refVideo3 = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_extend_video3.mp4";

        // Output video parameters
        final boolean generateAudio = true;
        final String videoRatio = "16:9";
        final long videoDuration = 8L;
        final boolean showWatermark = true;

        System.out.println("----- create request -----");
        // Build request content
        List<Content> contents = new ArrayList<>();

        // 1. Text prompt
        contents.add(Content.builder()
                .type("text")
                .text(prompt)
                .build());

        // 2. Reference video 1
        contents.add(Content.builder()
                .type("video_url")
                .videoUrl(CreateContentGenerationTaskRequest.VideoUrl.builder()
                        .url(refVideo1)
                        .build())
                .role("reference_video")
                .build());

        // 3. Reference video 2
        contents.add(Content.builder()
                .type("video_url")
                .videoUrl(CreateContentGenerationTaskRequest.VideoUrl.builder()
                        .url(refVideo2)
                        .build())
                .role("reference_video")
                .build());

        // 4. Reference video 3
        contents.add(Content.builder()
                .type("video_url")
                .videoUrl(CreateContentGenerationTaskRequest.VideoUrl.builder()
                        .url(refVideo3)
                        .build())
                .role("reference_video")
                .build());

        // Create video generation task
        CreateContentGenerationTaskRequest createRequest = CreateContentGenerationTaskRequest.builder()
                .generateAudio(generateAudio)
                .model(modelId)
                .content(contents)
                .ratio(videoRatio)
                .duration(videoDuration)
                .watermark(showWatermark)
                .build();

        CreateContentGenerationTaskResult createResult = service.createContentGenerationTask(createRequest);
        System.out.println("Task Created: " + createResult);

        // Get task details and poll status
        String taskId = createResult.getId();
        pollTaskStatus(taskId);
    \}

    /**
     * Poll task status
     *@param taskId Task ID
     */

    private static void pollTaskStatus(String taskId) \{
        GetContentGenerationTaskRequest getRequest = GetContentGenerationTaskRequest.builder()
                .taskId(taskId)
                .build();

        System.out.println("----- polling task status -----");
        try \{
            while (true) \{
                GetContentGenerationTaskResponse getResponse = service.getContentGenerationTask(getRequest);
                String status = getResponse.getStatus();

                if ("succeeded".equalsIgnoreCase(status)) \{
                    System.out.println("----- task succeeded -----");
                    System.out.println(getResponse);
                    break;
                \} else if ("failed".equalsIgnoreCase(status)) \{
                    System.out.println("----- task failed -----");
                    if (getResponse.getError() != null) \{
                        System.out.println("Error: " + getResponse.getError().getMessage());
                    \}
                    break;
                \} else \{
                    System.out.printf("Current status: %s, Retrying in 10 seconds...%n", status);
                    TimeUnit.SECONDS.sleep(10);
                \}
            \}
        \} catch (InterruptedException ie) \{
            Thread.currentThread().interrupt();
            System.err.println("Polling interrupted");
        \} catch (Exception e) \{
            System.err.println("Error occurred: " + e.getMessage());
        \} finally \{
            service.shutdownExecutor();
        \}
    \}
\}
\`\`\`

`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="Go" key="KbRH6lTg3a"><RenderMd content={`\`\`\`Go
package main

import (
    "context"
    "fmt"
    "os"
    "time"

    "github.com/byteplus-sdk/byteplus-go-sdk-v2/service/arkruntime"
    "github.com/byteplus-sdk/byteplus-go-sdk-v2/service/arkruntime/model"
    "github.com/byteplus-sdk/byteplus-go-sdk-v2/byteplus"
)

func main() \{
    // Initialize Ark client
    client := arkruntime.NewClientWithApiKey(
        os.Getenv("ARK_API_KEY"),
        //The base URL for model invocation
        arkruntime.WithBaseUrl("https://ark.ap-southeast.bytepluses.com/api/v3"),
    )
    ctx := context.Background()

    // Model ID
    modelID := "dreamina-seedance-2-0-260128"
    // Text prompt
    prompt := "The arched window in [video 1] opens, and the camera moves into the interior of the art museum, transitioning into [video 2]. After that, the camera enters the painting itself, transitioning into [video 3]."

    // Example resource URLs
    refVideo1 := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_extend_video1.mp4"
    refVideo2 := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_extend_video2.mp4"
    refVideo3 := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/r2v_extend_video3.mp4"

    // Output video parameters
    generateAudio := true
    videoRatio := "16:9"
    videoDuration := int64(8)
    showWatermark := true

    // 1. Create video generation task
    fmt.Println("----- create request -----")
    createReq := model.CreateContentGenerationTaskRequest\{
        Model:         modelID,
        GenerateAudio: byteplus.Bool(generateAudio),
        Ratio:         byteplus.String(videoRatio),
        Duration:      byteplus.Int64(videoDuration),
        Watermark:     byteplus.Bool(showWatermark),
        Content: []*model.CreateContentGenerationContentItem\{
            \{
                Type: model.ContentGenerationContentItemTypeText,
                Text: byteplus.String(prompt),
            \},
            \{
                Type: model.ContentGenerationContentItemType("video_url"),
                VideoURL: &model.VideoUrl\{
                    Url: refVideo1,
                \},
                Role: byteplus.String("reference_video"),
            \},
            \{
                Type: model.ContentGenerationContentItemType("video_url"),
                VideoURL: &model.VideoUrl\{
                    Url: refVideo2,
                \},
                Role: byteplus.String("reference_video"),
            \},
            \{
                Type: model.ContentGenerationContentItemType("video_url"),
                VideoURL: &model.VideoUrl\{
                    Url: refVideo3,
                \},
                Role: byteplus.String("reference_video"),
            \},
        \},
    \}

    createResp, err := client.CreateContentGenerationTask(ctx, createReq)
    if err != nil \{
        fmt.Printf("create content generation error: %v\\\\n", err)
        return
    \}

    taskID := createResp.ID
    fmt.Printf("Task Created with ID: %s\\\\n", taskID)

    // 2. Poll task status
    pollTaskStatus(ctx, client, taskID)
\}

// poll task status
func pollTaskStatus(ctx context.Context, client *arkruntime.Client, taskID string) \{
    fmt.Println("----- polling task status -----")
    for \{
        getReq := model.GetContentGenerationTaskRequest\{ID: taskID\}
        getResp, err := client.GetContentGenerationTask(ctx, getReq)
        if err != nil \{
            fmt.Printf("get content generation task error: %v\\\\n", err)
            return
        \}

        status := getResp.Status
        if status == "succeeded" \{
            fmt.Println("----- task succeeded -----")
            fmt.Printf("Task ID: %s \\\\n", getResp.ID)
            fmt.Printf("Model: %s \\\\n", getResp.Model)
            fmt.Printf("Video URL: %s \\\\n", getResp.Content.VideoURL)
            fmt.Printf("Completion Tokens: %d \\\\n", getResp.Usage.CompletionTokens)
            fmt.Printf("Created At: %d, Updated At: %d\\\\n", getResp.CreatedAt, getResp.UpdatedAt)
            return
        \} else if status == "failed" \{
            fmt.Println("----- task failed -----")
            if getResp.Error != nil \{
                fmt.Printf("Error Code: %s, Message: %s\\\\n", getResp.Error.Code, getResp.Error.Message)
            \}
            return
        \} else \{
            fmt.Printf("Current status: %s, Retrying in 10 seconds... \\\\n", status)
            time.Sleep(10 * time.Second)
        \}
    \}
\}
\`\`\`

`}></RenderMd></Tabs.TabPane></Tabs>);
```

:::tip tip

* When you extend a video clip forward or backward, the generated video usually only includes the tail footage of the original video. But you can also flexibly control it via a prompt to make it include the original video content. For example: Extend Video 1 backward, [description of the extended content...], and **then end with Video 1**.
* When you pass 2 to 3 video clips to fill in the intermediate transition part, the generated video will include both the original video content and the newly generated video content.

:::
<span id="17c64b2e"></span>
## More capabilities
Seedance 2.0 series models also support common basic capabilities such as text\-to\-video, first\-frame image\-to\-video, first\-and\-last\-frame image\-to\-video, and video output specification configuration. For details, see [Video generation tutorial](/docs/ModelArk/2298881).
<span id="5c67c9a1"></span>
# Create with ease
Seedance 2.0 series models also support common basic capabilities such as text\-to\-video, first\-frame image\-to\-video, first\-and\-last\-frame image\-to\-video, and video output specification configuration. For details, see [Video generation tutorial](/docs/ModelArk/2298881).

<span aceTableMode="list" aceTableWidth="1,3"></span>
|Solution |Overview |
|---|---|
|[Trusted outputs as input assets](/docs/ModelArk/2291680#c24c4bc5) |Original face\-containing outputs generated by some models under your account can be used as input assets of Seedance 2.0 series models without being intercepted by input moderation. |
|[Preset digital characters](/docs/ModelArk/2291680#2bf01416) |ModelArk has a pre\-built digital character library that provides creatives with free, compliant, and diverse portrait assets. It is suitable for scenarios that require realistic but not specific human faces, and pursue zero compliance risk and fast creation. |
|[Authorized real-person assets](/docs/ModelArk/2291680#86c3831f) |Supports video generation using authorized real portrait assets. |

<span id="c24c4bc5"></span>
## Trusted outputs as input assets
Seedance 2.0 series models do not support direct upload of reference images or videos containing real human faces. To make it easier for creatives to use human faces for derivative works, ModelArk platform trusts face\-containing outputs generated by the following models. You can use **original face\-containing outputs generated by the following models under your account in the last 30 days** as input assets of Seedance 2.0 series models again for derivative creation.

<span aceTableMode="list" aceTableWidth="3,3,2"></span>
|**Scope of trusted outputs** |**Earliest generation time of trusted outputs** |**Trust expires after** |
|---|---|---|
|Face\-containing videos generated by Seedance 2.0 series models |March 11, 2026 |30 days |
|Last frame images of face\-containing videos generated by Seedance 2.0 series models |April 16, 2026 |30 days |
|Face\-containing images generated by [Dola Seedream 5.0 Lite text to image](https://docs.byteplus.com/en/docs/ModelArk/1824121#text-to-image-text-input-single-image-output) |April 16, 2026 |30 days |

:::warning warning

* Only outputs generated by ModelArk are trusted, while outputs from other platforms are not supported.
* Only outputs generated under the same account are trusted, while cross\-account use is not supported.
* Only original model outputs are trusted. Modified or expired outputs cannot be used as input assets.
* Even if the input assets are trusted, outputs may still fail if they violate ModelArk security moderation policies. For details, see [Error codes](https://docs.byteplus.com/en/docs/ModelArk/1299023).
* The trust policy is for use cases involving human faces only. For use cases that do not use human faces, there is no trust issue with model outputs, and you can create or modify as needed.


:::
<span aceTableMode="list" aceTableWidth="1,2"></span>
|**Input: video generated under the same account** |**Output** |
|---|---|
|<video src="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/764dcd93aaa64589b114a37d6abbb254~tplv-goo7wpa0wc-image.image" controls></video>|<video src="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/bc43ce4b4fe9478b9c952889d4b98e8e~tplv-goo7wpa0wc-image.image" controls></video>|\
|||\
|> Video generated with [Preset digital characters](/docs/ModelArk/2291680#2bf01416) |> Input: Change the color of the cream to white.|\
| |> Change the ratio to 16:9. |


1. Generate an initial video, and obtain the video URL. Here we directly use the example from [Preset digital characters](/docs/ModelArk/2291680#2bf01416).
2. Edit the video generated by Seedance 2.0 again. The original video URL is only valid for 24 hours. In this example, the original video is transferred to BytePlus TOS for use.
   :::tip tip
   The original video URL is only valid for 24 hours. For actual use, it is recommended that you transfer and save the video file in advance. It is recommended to configure the data subscription function provided by BytePlus TOS to automatically transfer your video outputs to your own TOS bucket for long\-term backup or further processing. For details, see [Data subscription](https://docs.byteplus.com/en/docs/tos/Data_subscription).

   :::
```mixin-react
return (<Tabs>
<Tabs.TabPane title="Python" key="klXhwCnQRY"><RenderMd content={`\`\`\`Python
import os
import time
# Install SDK:  pip install byteplus-python-sdk-v2
from byteplussdkarkruntime import Ark

client = Ark(
    # The base URL for model invocation
    base_url='https://ark.ap-southeast.bytepluses.com/api/v3',
    # Get API Key: https://console.byteplus.com/ark/region:ark+ap-southeast-1/apikey
    api_key=os.environ.get("ARK_API_KEY"),
)

if __name__ == "__main__":
    print("----- create request -----")
    create_result = client.content_generation.tasks.create(
        model="dreamina-seedance-2-0-260128", # Replace with Model ID
        content=[
            \{
                "type": "text",
                "text": "Change the color of the cream to white."
            \},
            \{
                "type": "video_url",
                "video_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/video_by_sd2.mp4"
                \},
                "role": "reference_video"
            \},
        ],
        generate_audio=True,
        ratio="16:9",
        duration=11,
        watermark=True,
    )
    print(create_result)
    print("----- polling task status -----")
    task_id = create_result.id
    while True:
        get_result = client.content_generation.tasks.get(task_id=task_id)
        status = get_result.status
        if status == "succeeded":
            print("----- task succeeded -----")
            print(get_result)
            break
        elif status == "failed":
            print("----- task failed -----")
            print(f"Error: \{get_result.error\}")
            break
        else:
            print(f"Current status: \{status\}, Retrying after 30 seconds...")
            time.sleep(30)
\`\`\`

`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="Java" key="jOra8XjvJx"><RenderMd content={`\`\`\`Java
package com.ark.sample;

import com.byteplus.ark.runtime.model.content.generation.*;
import com.byteplus.ark.runtime.model.content.generation.CreateContentGenerationTaskRequest.Content;
import com.byteplus.ark.runtime.service.ArkService;
import okhttp3.ConnectionPool;
import okhttp3.Dispatcher;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

public class ContentGenerationTaskExample \{

    // Client initialization
    static String apiKey = System.getenv("ARK_API_KEY");
    static ConnectionPool connectionPool = new ConnectionPool(5, 1, TimeUnit.SECONDS);
    static Dispatcher dispatcher = new Dispatcher();
    static ArkService service = ArkService.builder()
           .baseUrl("https://ark.ap-southeast.bytepluses.com/api/v3") // The base URL for model invocation
           .dispatcher(dispatcher)
           .connectionPool(connectionPool)
           .apiKey(apiKey)
           .build();

    public static void main(String[] args) \{

        // Model ID
        final String modelId = "dreamina-seedance-2-0-260128";
        // Text prompt
        final String prompt = "Change the color of the cream to white.";

        // Example resource URLs
        final String refVideo = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/video_by_sd2.mp4";

        // Output video parameters
        final boolean generateAudio = true;
        final String videoRatio = "16:9";
        final long videoDuration = 11L;
        final boolean showWatermark = true;

        System.out.println("----- create request -----");
        // Build request content
        List<Content> contents = new ArrayList<>();

        // 1. Text prompt
        contents.add(Content.builder()
                .type("text")
                .text(prompt)
                .build());

        // 2. Reference video
        contents.add(Content.builder()
                .type("video_url")
                .videoUrl(CreateContentGenerationTaskRequest.VideoUrl.builder()
                        .url(refVideo)
                        .build())
                .role("reference_video")
                .build());

        // Create video generation task
        CreateContentGenerationTaskRequest createRequest = CreateContentGenerationTaskRequest.builder()
                .generateAudio(generateAudio)
                .model(modelId)
                .content(contents)
                .ratio(videoRatio)
                .duration(videoDuration)
                .watermark(showWatermark)
                .build();

        CreateContentGenerationTaskResult createResult = service.createContentGenerationTask(createRequest);
        System.out.println("Task Created: " + createResult);

        // Get task details and poll status
        String taskId = createResult.getId();
        pollTaskStatus(taskId);
    \}

    /**
     * Poll task status
     * @param taskId Task ID
     */

    private static void pollTaskStatus(String taskId) \{
        GetContentGenerationTaskRequest getRequest = GetContentGenerationTaskRequest.builder()
                .taskId(taskId)
                .build();

        System.out.println("----- polling task status -----");
        try \{
            while (true) \{
                GetContentGenerationTaskResponse getResponse = service.getContentGenerationTask(getRequest);
                String status = getResponse.getStatus();

                if ("succeeded".equalsIgnoreCase(status)) \{
                    System.out.println("----- task succeeded -----");
                    System.out.println(getResponse);
                    break;
                \} else if ("failed".equalsIgnoreCase(status)) \{
                    System.out.println("----- task failed -----");
                    if (getResponse.getError() != null) \{
                        System.out.println("Error: " + getResponse.getError().getMessage());
                    \}
                    break;
                \} else \{
                    System.out.printf("Current status: %s, Retrying in 10 seconds...%n", status);
                    TimeUnit.SECONDS.sleep(10);
                \}
            \}
        \} catch (InterruptedException ie) \{
            Thread.currentThread().interrupt();
            System.err.println("Polling interrupted");
        \} catch (Exception e) \{
            System.err.println("Error occurred: " + e.getMessage());
        \} finally \{
            service.shutdownExecutor();
        \}
    \}
\}
\`\`\`

`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="Go" key="QZNAIdzq7w"><RenderMd content={`\`\`\`Go
package main

import (
    "context"
    "fmt"
    "os"
    "time"

    "github.com/byteplus-sdk/byteplus-go-sdk-v2/service/arkruntime"
    "github.com/byteplus-sdk/byteplus-go-sdk-v2/service/arkruntime/model"
    "github.com/byteplus-sdk/byteplus-go-sdk-v2/byteplus"
)

func main() \{
    // Initialize Ark client
    client := arkruntime.NewClientWithApiKey(
        os.Getenv("ARK_API_KEY"),
        // The base URL for model invocation
        arkruntime.WithBaseUrl("https://ark.ap-southeast.bytepluses.com/api/v3"),
    )
    ctx := context.Background()

    // Model ID
    modelID := "dreamina-seedance-2-0-260128"
    // Text prompt
    prompt := "Change the color of the cream to white."

    // Example resource URLs
    refVideo1 := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_video/video_by_sd2.mp4"

    // Output video parameters
    generateAudio := true
    videoRatio := "16:9"
    videoDuration := int64(11)
    showWatermark := true

    // 1. Create video generation task
    fmt.Println("----- create request -----")
    createReq := model.CreateContentGenerationTaskRequest\{
        Model:         modelID,
        GenerateAudio: byteplus.Bool(generateAudio),
        Ratio:         byteplus.String(videoRatio),
        Duration:      byteplus.Int64(videoDuration),
        Watermark:     byteplus.Bool(showWatermark),
        Content: []*model.CreateContentGenerationContentItem\{
            \{
                Type: model.ContentGenerationContentItemTypeText,
                Text: byteplus.String(prompt),
            \},
            \{
                Type: model.ContentGenerationContentItemType("video_url"),
                VideoURL: &model.VideoUrl\{
                    Url: refVideo1,
                \},
                Role: byteplus.String("reference_video"),
            \},
        \},
    \}

    createResp, err := client.CreateContentGenerationTask(ctx, createReq)
    if err != nil \{
        fmt.Printf("create content generation error: %v\\\\n", err)
        return
    \}

    taskID := createResp.ID
    fmt.Printf("Task Created with ID: %s\\\\n", taskID)

    // 2. Poll task status
    pollTaskStatus(ctx, client, taskID)
\}

// poll task status
func pollTaskStatus(ctx context.Context, client *arkruntime.Client, taskID string) \{
    fmt.Println("----- polling task status -----")
    for \{
        getReq := model.GetContentGenerationTaskRequest\{ID: taskID\}
        getResp, err := client.GetContentGenerationTask(ctx, getReq)
        if err != nil \{
            fmt.Printf("get content generation task error: %v\\\\n", err)
            return
        \}

        status := getResp.Status
        if status == "succeeded" \{
            fmt.Println("----- task succeeded -----")
            fmt.Printf("Task ID: %s \\\\n", getResp.ID)
            fmt.Printf("Model: %s \\\\n", getResp.Model)
            fmt.Printf("Video URL: %s \\\\n", getResp.Content.VideoURL)
            fmt.Printf("Completion Tokens: %d \\\\n", getResp.Usage.CompletionTokens)
            fmt.Printf("Created At: %d, Updated At: %d\\\\n", getResp.CreatedAt, getResp.UpdatedAt)
            return
        \} else if status == "failed" \{
            fmt.Println("----- task failed -----")
            if getResp.Error != nil \{
                fmt.Printf("Error Code: %s, Message: %s\\\\n", getResp.Error.Code, getResp.Error.Message)
            \}
            return
        \} else \{
            fmt.Printf("Current status: %s, Retrying in 10 seconds... \\\\n", status)
            time.Sleep(10 * time.Second)
        \}
    \}
\}
\`\`\`

`}></RenderMd></Tabs.TabPane></Tabs>);
```

<span id="2bf01416"></span>
## Preset digital characters
For realistic style videos, you can control the character appearance through pre\-built avatars in the digital character library. Each asset has a unique asset ID. You can generate a video by passing `asset://<asset ID>` in the **content._url.url**parameter.
:::tip tip
To activate the digital character library or browse and search for digital characters, see [Digital character library](/docs/ModelArk/2223965).

:::
<span aceTableMode="list" aceTableWidth="3,2,2"></span>
|Input: text |Input: digital character, image |Output |
|---|---|---|
|Vertical HD close\-up video of a beauty blogger (Image 1). She has bold, glamorous makeup with no facial shine or glare and a sweet smile. She holds a face cream jar (Image 2), presents it directly to the camera. The background is fresh and minimalist. Energetic and sweet style. Character speaks in real\-time: 'I found my holy grail face cream! It has a cloud\-like creamy texture that absorbs instantly. Perfect for post\-all\-nighter rescue, deep hydration and moisturization—my skin glows naturally even without makeup!'"|<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/cf2763b55b124d53b9387f697b9c3ba2~tplv-goo7wpa0wc-image.image =300x) </span>|<video src="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/764dcd93aaa64589b114a37d6abbb254~tplv-goo7wpa0wc-image.image" controls></video>|\
|:::warning warning|> Digital character| |\
|The Asset ID is only used to pass assets to the model. Prompts must reference assets in the format **asset type + number**, where the number is the sorting order of the asset among assets of the same type in the request body.|| |\
|Correct usage: The beauty influencer in **Image 1**|<span>![图片](https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/791b783fc6cd4394b13f41b66b5ff461~tplv-goo7wpa0wc-image.image =200x) </span>| |\
|Incorrect usage: asset\-2026\*\*\*\* is a beauty influencer|> Product image | |\
|| | |\
|:::| | |


```mixin-react
return (<Tabs>
<Tabs.TabPane title="Python" key="qjGzlWNzRr"><RenderMd content={`\`\`\`Python
import os
import time
# Install SDK:pip install byteplus-python-sdk-v2
from byteplussdkarkruntime import Ark

client = Ark(
    #The base URL for model invocation
    base_url='https://ark.ap-southeast.bytepluses.com/api/v3',
    # Get API Key: https://console.byteplus.com/ark/region:ark+ap-southeast-1/apikey
    api_key=os.environ.get("ARK_API_KEY"),
)

if __name__ == "__main__":
    print("----- create request -----")
    create_result = client.content_generation.tasks.create(
        model="dreamina-seedance-2-0-260128", #Replace with Model ID
        content=[
            \{
                "type": "text",
                "text": "固定机位，近景镜头，清新自然风格。在室内自然光下，图片1中美妆博主面带笑容，向镜头介绍图片2中的面霜。博主将手里的面霜展示给镜头，开心地说“挖到本命面霜了！”；接着她一边用手指轻轻蘸取面霜展示那种软糯感，一边说“质地像云朵一样软糯，一抹就吸收”；最后她把面霜涂抹在脸颊上，展示着水润透亮的皮肤，同时自信地说“熬夜急救、补水保湿全搞定”。要求画面中人物居中，完整展示人物的整个脑袋和上半身，始终对焦人脸，人脸始终清晰，纯净无任何字幕。"
                "text": "Vertical HD close-up video of a beauty blogger (Image 1). She has bold, glamorous makeup with no facial shine or glare and a sweet smile. She holds a face cream jar (Image 2), presents it directly to the camera. The background is fresh and minimalist. Energetic and sweet style. Character speaks in real-time: 'I found my holy grail face cream! It has a cloud-like creamy texture that absorbs instantly. Perfect for post-all-nighter rescue, deep hydration and moisturization—my skin glows naturally even without makeup!'"
            \},
            \{
                "type": "image_url",
                "image_url": \{
                    "url": "asset://asset-20260401123823-6d4x2"
                    "url": "asset://asset-20260410114236-8cdfz"
                \},
                "role": "reference_image"
            \},
            \{
                "type": "image_url",
                "image_url": \{
                    "url": "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_edit_pic1.jpg"
                \},
                "role": "reference_image"
            \},
        ],
        generate_audio=True,
        ratio="adaptive",
        duration=11,
        watermark=True,
    )
    print(create_result)

    print("----- polling task status -----")
    task_id = create_result.id
    while True:
        get_result = client.content_generation.tasks.get(task_id=task_id)
        status = get_result.status
        if status == "succeeded":
            print("----- task succeeded -----")
            print(get_result)
            break
        elif status == "failed":
            print("----- task failed -----")
            print(f"Error: \{get_result.error\}")
            break
        else:
            print(f"Current status: \{status\}, Retrying after 30 seconds...")
            time.sleep(30)
\`\`\`

`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="Java" key="wnCPfaMTCH"><RenderMd content={`\`\`\`Java
package com.ark.sample;

import com.byteplus.ark.runtime.model.content.generation.*;
import com.byteplus.ark.runtime.model.content.generation.CreateContentGenerationTaskRequest.Content;
import com.byteplus.ark.runtime.service.ArkService;
import okhttp3.ConnectionPool;
import okhttp3.Dispatcher;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

public class ContentGenerationTaskExample \{

    // Client initialization
    static String apiKey = System.getenv("ARK_API_KEY");
    static ConnectionPool connectionPool = new ConnectionPool(5, 1, TimeUnit.SECONDS);
    static Dispatcher dispatcher = new Dispatcher();
    static ArkService service = ArkService.builder()
           .baseUrl("https://ark.ap-southeast.bytepluses.com/api/v3") //The base URL for model invocation
           .dispatcher(dispatcher)
           .connectionPool(connectionPool)
           .apiKey(apiKey)
           .build();

    public static void main(String[] args) \{

        // Model ID
        final String modelId = "dreamina-seedance-2-0-260128";
        // Text prompt
        final String prompt = "Vertical HD close-up video of a beauty blogger (Image 1). She has bold, glamorous makeup with no facial shine or glare and a sweet smile. She holds a face cream jar (Image 2), presents it directly to the camera. The background is fresh and minimalist. Energetic and sweet style. Character speaks in real-time: 'I found my holy grail face cream! It has a cloud-like creamy texture that absorbs instantly. Perfect for post-all-nighter rescue, deep hydration and moisturization—my skin glows naturally even without makeup!'";

        // Example resource URLs
        final String refImage2 = "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_edit_pic1.jpg";

        // Output video parameters
        final boolean generateAudio = true;
        final String videoRatio = "adaptive";
        final long videoDuration = 11L;
        final boolean showWatermark = true;

        System.out.println("----- create request -----");
        // Build request content
        List<Content> contents = new ArrayList<>();

        // 1. Text prompt
        contents.add(Content.builder()
                .type("text")
                .text(prompt)
                .build());

        // 2. Reference image 1
        contents.add(Content.builder()
                .type("image_url")
                .imageUrl(CreateContentGenerationTaskRequest.ImageUrl.builder()
                        .url(refImage1)
                        .build())
                .role("reference_image")
                .build());

        // 3. Reference image 2
        contents.add(Content.builder()
                .type("image_url")
                .imageUrl(CreateContentGenerationTaskRequest.ImageUrl.builder()
                        .url(refImage2)
                        .build())
                .role("reference_image")
                .build());

        // Create video generation task
        CreateContentGenerationTaskRequest createRequest = CreateContentGenerationTaskRequest.builder()
                .generateAudio(generateAudio)
                .model(modelId)
                .content(contents)
                .ratio(videoRatio)
                .duration(videoDuration)
                .watermark(showWatermark)
                .build();

        CreateContentGenerationTaskResult createResult = service.createContentGenerationTask(createRequest);
        System.out.println("Task Created: " + createResult);

        // Get task details and poll status
        String taskId = createResult.getId();
        pollTaskStatus(taskId);
    \}

    /**
     * Poll task status
     *@param taskId Task ID
     */

    private static void pollTaskStatus(String taskId) \{
        GetContentGenerationTaskRequest getRequest = GetContentGenerationTaskRequest.builder()
                .taskId(taskId)
                .build();

        System.out.println("----- polling task status -----");
        try \{
            while (true) \{
                GetContentGenerationTaskResponse getResponse = service.getContentGenerationTask(getRequest);
                String status = getResponse.getStatus();

                if ("succeeded".equalsIgnoreCase(status)) \{
                    System.out.println("----- task succeeded -----");
                    System.out.println(getResponse);
                    break;
                \} else if ("failed".equalsIgnoreCase(status)) \{
                    System.out.println("----- task failed -----");
                    if (getResponse.getError() != null) \{
                        System.out.println("Error: " + getResponse.getError().getMessage());
                    \}
                    break;
                \} else \{
                    System.out.printf("Current status: %s, Retrying in 10 seconds...%n", status);
                    TimeUnit.SECONDS.sleep(10);
                \}
            \}
        \} catch (InterruptedException ie) \{
            Thread.currentThread().interrupt();
            System.err.println("Polling interrupted");
        \} catch (Exception e) \{
            System.err.println("Error occurred: " + e.getMessage());
        \} finally \{
            service.shutdownExecutor();
        \}
    \}
\}
\`\`\`

`}></RenderMd></Tabs.TabPane>
<Tabs.TabPane title="Go" key="nIVlCiBrPe"><RenderMd content={`\`\`\`Go
package main

import (
    "context"
    "fmt"
    "os"
    "time"

    "github.com/byteplus-sdk/byteplus-go-sdk-v2/service/arkruntime"
    "github.com/byteplus-sdk/byteplus-go-sdk-v2/service/arkruntime/model"
    "github.com/byteplus-sdk/byteplus-go-sdk-v2/byteplus"
)

func main() \{
    // Initialize Ark client
    client := arkruntime.NewClientWithApiKey(
        os.Getenv("ARK_API_KEY"),
        //The base URL for model invocation
        arkruntime.WithBaseUrl("https://ark.ap-southeast.bytepluses.com/api/v3"),
    )
    ctx := context.Background()

    // Model ID
    modelID := "dreamina-seedance-2-0-260128"
    // Text prompt
    prompt := "Vertical HD close-up video of a beauty blogger (Image 1). She has bold, glamorous makeup with no facial shine or glare and a sweet smile. She holds a face cream jar (Image 2), presents it directly to the camera. The background is fresh and minimalist. Energetic and sweet style. Character speaks in real-time: 'I found my holy grail face cream! It has a cloud-like creamy texture that absorbs instantly. Perfect for post-all-nighter rescue, deep hydration and moisturization—my skin glows naturally even without makeup!'"

    // Example resource URLs
    refImage2 := "https://ark-doc.tos-ap-southeast-1.bytepluses.com/doc_image/r2v_edit_pic1.jpg"

    // Output video parameters
    generateAudio := true
    videoRatio := "adaptive"
    videoDuration := int64(11)
    showWatermark := true

    // 1. Create video generation task
    fmt.Println("----- create request -----")
    createReq := model.CreateContentGenerationTaskRequest\{
        Model:         modelID,
        GenerateAudio: byteplus.Bool(generateAudio),
        Ratio:         byteplus.String(videoRatio),
        Duration:      byteplus.Int64(videoDuration),
        Watermark:     byteplus.Bool(showWatermark),
        Content: []*model.CreateContentGenerationContentItem\{
            \{
                Type: model.ContentGenerationContentItemTypeText,
                Text: byteplus.String(prompt),
            \},
            \{
                Type: model.ContentGenerationContentItemType("image_url"),
                ImageURL: &model.ImageURL\{
                    URL: refImage1,
                \},
                Role: byteplus.String("reference_image"),
            \},
            \{
                Type: model.ContentGenerationContentItemType("image_url"),
                ImageURL: &model.ImageURL\{
                    URL: refImage2,
                \},
                Role: byteplus.String("reference_image"),
            \},
        \},
    \}

    createResp, err := client.CreateContentGenerationTask(ctx, createReq)
    if err != nil \{
        fmt.Printf("create content generation error: %v\\\\n", err)
        return
    \}

    taskID := createResp.ID
    fmt.Printf("Task Created with ID: %s\\\\n", taskID)

    // 2. Poll task status
    pollTaskStatus(ctx, client, taskID)
\}

// poll task status
func pollTaskStatus(ctx context.Context, client *arkruntime.Client, taskID string) \{
    fmt.Println("----- polling task status -----")
    for \{
        getReq := model.GetContentGenerationTaskRequest\{ID: taskID\}
        getResp, err := client.GetContentGenerationTask(ctx, getReq)
        if err != nil \{
            fmt.Printf("get content generation task error: %v\\\\n", err)
            return
        \}

        status := getResp.Status
        if status == "succeeded" \{
            fmt.Println("----- task succeeded -----")
            fmt.Printf("Task ID: %s \\\\n", getResp.ID)
            fmt.Printf("Model: %s \\\\n", getResp.Model)
            fmt.Printf("Video URL: %s \\\\n", getResp.Content.VideoURL)
            fmt.Printf("Completion Tokens: %d \\\\n", getResp.Usage.CompletionTokens)
            fmt.Printf("Created At: %d, Updated At: %d\\\\n", getResp.CreatedAt, getResp.UpdatedAt)
            return
        \} else if status == "failed" \{
            fmt.Println("----- task failed -----")
            if getResp.Error != nil \{
                fmt.Printf("Error Code: %s, Message: %s\\\\n", getResp.Error.Code, getResp.Error.Message)
            \}
            return
        \} else \{
            fmt.Printf("Current status: %s, Retrying in 10 seconds... \\\\n", status)
            time.Sleep(10 * time.Second)
        \}
    \}
\}
\`\`\`

`}></RenderMd></Tabs.TabPane></Tabs>);
```

<span id="86c3831f"></span>
## Authorized real\-person assets
After passing real\-person verification and obtaining personal authorization, you can upload relevant assets of the real person (such as images, videos, and audio of the real person) to ModelArk. After the asset is successfully registered, each asset will get an independent Asset ID. You can pass `asset://<asset ID>` in the **content._url.url**parameter to use this asset to generate videos. For the real\-person verification and asset registration process, see [Add real-human assets to asset library](/docs/ModelArk/2315856).
```JSON
...
"content": [
         {
            "type": "text",
            "text": "<your prompt>"
        },
        {
            "type": "image_url",
            "image_url": {
                "url": "asset://<asset ID>"
            },
            "role": "reference_image"
        },
        {
            "type": "video_url",
            "video_url": {
                "url": "asset://<asset ID>"
            },
            "role": "reference_video"
        },
        {
            "type": "audio_url",
            "audio_url": {
                "url": "asset://<asset ID>"
            },
            "role": "reference_audio"
        }
    ]
...
```

<span id="7f69bcbf"></span>
# Prompt engineering techniques
Prompts must reference assets in the format **asset type + number**, where the number is the sorting order of the asset among assets of the same type in the request body. For example, "Image n" refers to the nth reference image with `type="image_url"` in the `content` array (counting starts from 1 in array order). **Note that referencing assets by Asset ID is not supported.**
The following section describes typical prompt formulas for multimodal reference, video editing, and video extension. For more details, see [Dreamina Seedance 2.0 series prompt guide](/docs/ModelArk/2222480).
:::tip tip
ModelArk provides the **Seedance 2.0 prompt optimization skill** to help you tune your prompts.

* How to install: You can configure the skill file in Code Agent / AI Agent to use it. Take OpenClaw as an example: download the SKILL.md file, copy the full content to the dialog input box, send "Please install this skill", and wait for the tool to complete the installation automatically.
* How to use: Enter `/sd2-pe + your prompt content` in the AI dialog box to start debugging prompts.
   <Attachment link="https://p9-arcosite.byteimg.com/tos-cn-i-goo7wpa0wc/4ce1ff014f674815bd7fcf321b404584~tplv-goo7wpa0wc-image.image" name="SKILL.md">SKILL.md</Attachment>


:::
<span id="b34e43cc"></span>
## **Multimodal reference**

* Image reference: Reference / extract / combine + "subject / referenced element description" from "Image n" to generate "plot description", keeping the characteristics of "subject / referenced element description" consistent.
* Video reference: Reference "action description / camera movement description / special effect description" from "Video n" to generate "plot description", keeping the action details / camera movement / special effects consistent.
* Audio reference:
   * Voice timbre reference: "Character" says: "lines", voice timbre references "Audio n".
   * Audio content reference: Ideal appearance timing + "Audio n".

<span id="bd0f005e"></span>
## **Edit video**

* Add elements: Clearly describe "element characteristics" + "appearance timing" + "appearance position".
* Delete elements: Specify the elements to be deleted, and emphasize the elements that remain unchanged in the prompt for better result.
* Modify elements: Simply clearly describe the elements to be replaced.

<span id="d7a8f9f2"></span>
## **Extend video**

* Extend video: Extend "Video n" forward/backward + "description of the video to be extended".
* Track completion: "Video 1" + "transition description" + connect to "Video 2" + "transition description" + connect to "Video 3".

<span id="66cb028f"></span>
# Limitations
See [Limitations](/docs/ModelArk/2298881#66cb028f).
